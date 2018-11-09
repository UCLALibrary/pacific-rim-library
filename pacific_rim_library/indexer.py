#!/usr/bin/python3

"""Indexer for PRL."""

import argparse
from io import TextIOWrapper
import json
from json import JSONDecodeError
import logging
import logging.config
from mimetypes import guess_extension
import os
import time
import toml
from typing import Any, Dict
import urllib
import yaml

import boto3
from botocore.exceptions import BotoCoreError, ProfileNotFound
from bs4 import BeautifulSoup
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from javaobj import JavaObjectUnmarshaller
import plyvel
from pysolr import Solr
import requests
from sickle import Sickle
from watchdog.observers import Observer

from pacific_rim_library.configure import DEFAULTS, get_config
from pacific_rim_library.prl_solr_document import PRLSolrDocument
from pacific_rim_library.indexer_event_handler import IndexerEventHandler
from pacific_rim_library.harvest_settings_event_handler import HarvestSettingsEventHandler
from pacific_rim_library.errors import IndexerError


class Indexer(object):
    """Indexer for PRL."""

    def __init__(self, args: Dict[str, Any], config: Dict[str, Any]):

        self.solr = None
        self.s3 = None
        self.record_identifiers = None
        self.harvester_settings = None

        self.args = args
        self.config = config
        self.oai_pmh_cache = {}

    def connect(self):
        """Initializes the interfaces for all third-party services."""

        self._connect_internal_services()
        if not self.args['dry_run']:
            self._connect_external_services()

    def _connect_internal_services(self):
        """Initializes the interfaces for all third-party services instantiated by this module."""

        try:
            self.record_identifiers = plyvel.DB(
                os.path.expanduser(self.config['leveldb']['record_identifiers']['path']),
                create_if_missing=True
            )
            self.harvester_settings = plyvel.DB(
                os.path.expanduser(self.config['leveldb']['harvester_settings']['path']),
                create_if_missing=True
            )
            self.set_harvester_settings()
        except plyvel.IOError as e:
            raise IndexerError('Failed to instantiate LevelDB instance: {}'.format(repr(e)))

    def _connect_external_services(self):
        """Initializes the interfaces for all third-party services NOT instantiated by this module."""

        try:
            solr_base_url = self.config['solr']['base_url']
            URLValidator()(solr_base_url)
            self.solr = Solr(solr_base_url)
            self.s3 = boto3.Session(
                profile_name=self.config['s3']['configure']['profile_name']
            ).client('s3')
        except ValidationError:
            raise IndexerError('Solr base URL {} is invalid'.format(solr_base_url))
        except ProfileNotFound as e:
            raise IndexerError('Failed to initialize S3 session: {}'.format(repr(e)))

    def disconnect(self):
        """Closes connections with all third-party services."""

        self._disconnect_internal_services()
        if not self.args['dry_run']:
            self._disconnect_external_services()

    def _disconnect_internal_services(self):
        """Closes connections with all third-party services instantiated by this module."""

        try:
            self.record_identifiers.close()
            self.harvester_settings.close()
        except plyvel.Error as e:
            raise IndexerError('Failed to close the connection to LevelDB: {}'.format(e))

    def _disconnect_external_services(self):
        """Closes connections with all third-party services NOT instantiated by this module."""

        self.solr = None
        self.s3 = None

    def get_harvester_settings_path(self) -> str:
        """Gets the full path of the file containing jOAI harvester settings."""

        return os.path.join(os.path.expanduser(
            self.config['leveldb']['harvester_settings']['source']['base_path']),
            self.config['leveldb']['harvester_settings']['source']['files']['scheduled_harvests'])

    def get_harvester_settings_key(self, path: str) -> str:
        """
        Returns a relative path with exactly two components.
        
        Intended to be called ONLY on paths representing collection/set directories.
        """

        collection_id = os.path.basename((path))
        institution_id = os.path.basename(os.path.dirname(path))
        return os.path.join(institution_id, collection_id)

    def read_harvester_settings_file(self) -> Dict[str, Dict[str, str]]:
        """Returns a dictionary representing the harvester settings.

        First, tries reading the settings as if the source file is UTF-8 encoded JSON of the following form (used for testing):

        {
            "harvester_settings_key_1": {
                "repository_name": "repository_name_1",
                "base_url": "http://example.edu/oai2",
                "set_spec": "set_spec_1"
            },
            ...
        }

        If that fails, tries reading the settings as if the source file is a serialized java.util.Hashtable instance from jOAI (used for production).
        """

        harvester_settings_path = self.get_harvester_settings_path()

        try:
            # See if it's in JSON already.
            with open(harvester_settings_path, 'r') as harvester_settings_file:
                # Make sure we transform the key before storing.
                return {
                    self.get_harvester_settings_key(key): metadata
                    for key, metadata in json.load(harvester_settings_file).items()
                }
        except JSONDecodeError as e:
            # Invalid JSON.
            raise IndexerError('Cannot load scheduled harvests settings: {}'.format(e))
        except UnicodeDecodeError as e:
            logging.info('Config file is not JSON: {}'.format(e))

            # Open the file in binary mode and try to parse it with javaobj.
            with open(harvester_settings_path, 'rb') as harvester_settings_file:
                pobj = JavaObjectUnmarshaller(harvester_settings_file).readObject()

            scheduled_harvest_class = self.config['leveldb']['harvester_settings']['source']['classes']['scheduled_harvest']
            is_scheduled_harvest = lambda h: scheduled_harvest_class in str(h)

            return {
                self.get_harvester_settings_key(pobj_harvest.harvestDir.path): {
                    'repository_name': pobj_harvest.repositoryName,
                    'base_url': pobj_harvest.baseURL,
                    'set_spec': pobj_harvest.setSpec
                }
                for pobj_harvest in list(filter(is_scheduled_harvest, pobj.annotations))
            }
        except Exception as e:
            # Something else went wrong.
            raise IndexerError('Cannot load scheduled harvests settings: {}'.format(e))

    def set_harvester_settings(self):
        """Updates the harvester_settings LevelDB instance with the data stored in the source file.
        
        Responds to filesystem event on that file.
        """

        new_harvester_settings = self.read_harvester_settings_file()
        deleted_keys = []
        updated_keys = []

        # Remove all keys from LevelDB that aren't in the harvester settings file.
        harvester_settings_iterator = self.harvester_settings.iterator()
        for key, value in harvester_settings_iterator:
            if key.decode() not in new_harvester_settings:
                self.harvester_settings.delete(key)
                deleted_keys.append(key)

        if deleted_keys:
            logging.info('Deleted harvester settings for %s', deleted_keys)

        # Add all keys in the harvester settings file to LevelDB, since some of their values may have changed.
        for harvest_key, harvest_metadata in new_harvester_settings.items():
            key = harvest_key
            value = json.dumps(harvest_metadata)
            self.harvester_settings.put(
                key.encode(),
                value.encode()
            )
            updated_keys.append(key)

        if updated_keys:
            logging.info('Updated harvester settings for %s', updated_keys)

    def update_record(self, path: str):
        """Updates a metadata record in PRL.
        
        Responds to IndexerEventHandler.on_modified filesystem event.
        """

        try:
            # Generate a Solr document from the metadata record.
            with open(path, 'r') as record_file:
                prl_solr_document = self.get_solr_document(record_file)
            pysolr_doc = prl_solr_document.get_pysolr_doc()
            record_identifier = prl_solr_document.get_record_identifier()

            if not self.args['dry_run']:
                try:
                    self.solr.add([pysolr_doc])
                    logging.debug('%s updated in Solr', record_identifier)
                    logging.debug(json.dumps(pysolr_doc, indent=4))
                    self.record_identifiers.put(path.encode(), record_identifier.encode())
                except plyvel.Error as e:
                    self.solr.delete(id=record_identifier)
                    raise IndexerError('Failed to PUT on LevelDB: {}'.format(e))
                except Exception as e:
                    raise IndexerError('Failed to update Solr document: {}'.format(e))

                if prl_solr_document.original_thumbnail_metadata() is not None:
                    self.save_thumbnail(prl_solr_document)

                logging.info('%s updated in PRL', record_identifier)
            else:
                logging.info('DRY-RUN: %s updated in PRL', record_identifier)
        except IndexerError as e:
            if self.args['dry_run']:
                logging.error(
                    'DRY-RUN: %s would not be updated in PRL: %s',
                    record_identifier,
                    e)
            else:
                raise e

    def remove_record(self, path: str):
        """Removes a metadata record from PRL.
        
        Responds to IndexerEventHandler.on_deleted filesystem event.
        """

        if not self.args['dry_run']:
            try:
                record_identifier = self.record_identifiers.get(path.encode()).decode()
                solr_escaped_record_identifier = record_identifier.replace(':', '\\:')
                docs = self.solr.search(
                    'id:{0}'.format(solr_escaped_record_identifier),
                    **{'rows': 1})
                if len(docs) != 1:
                    raise IndexerError('Solr doesn\'t have unique IDs')
            except plyvel.Error as e:
                raise IndexerError('Failed to GET on LevelDB: {}'.format(e))
            except Exception as e:
                raise IndexerError('Failed to search for Solr document: {}'.format(e))

            try:
                self.solr.delete(id=record_identifier)
                logging.debug('%s removed from Solr', record_identifier)
                self.record_identifiers.delete(path.encode())
                for doc in docs:
                    if 'thumbnail_url' in doc:
                        self.unsave_thumbnail(record_identifier)
                logging.info('%s removed from PRL', record_identifier)
            except plyvel.Error as e:
                raise IndexerError('Failed to DELETE on LevelDB: {}'.format(e))
            except Exception as e:
                raise IndexerError('Failed to remove Solr document: {}'.format(e))
        else:
            logging.info('DRY-RUN: Removed %s', path)

    def get_oai_pmh_sets(self, base_url: str) -> Dict[str, str]:
        """Returns a dictionary that maps OAI-PMH setSpecs to setNames."""

        logging.debug('Listing sets from OAI-PMH repository %s', base_url)
        try:
            return {
                s.setSpec: s.setName
                for s in list(Sickle(base_url, timeout=60).ListSets())
            }
        except requests.RequestException as e:
            raise IndexerError('Failed to list sets from OAI-PMH repository {}: {}'.format(base_url, e))

    def get_solr_document(self, file_object: TextIOWrapper) -> PRLSolrDocument:
        """Builds a Solr document for PRL."""

        # Get all we can from the filename.
        file_path = file_object.name
        identifier = urllib.parse.unquote(os.path.splitext(os.path.basename(file_path))[0])
        institution_key = os.path.basename(os.path.dirname(os.path.dirname(file_path)))

        harvester_settings_key = self.get_harvester_settings_key(os.path.dirname(file_path))
        try:
            harvester_settings_serialized_encoded = self.harvester_settings.get(harvester_settings_key.encode())
            harvester_settings_serialized = harvester_settings_serialized_encoded.decode()
            harvester_settings = json.loads(harvester_settings_serialized)
        except plyvel.Error as e:
            # We can't go on without LevelDB.
            raise IndexerError('Failed to GET on LevelDB: {}'.format(e))
        except AttributeError as e:
            # This should never happen. Harvester settings should represent all harvested files.
            raise IndexerError('Cannot find harvester settings in LevelDB for {}'.format(harvester_settings_key))
        except JSONDecodeError as e:
            # This should never happen.
            raise IndexerError('Harvester settings are not valid JSON: {}'.format(e))

        base_url = harvester_settings['base_url']
        collection_key = harvester_settings['set_spec'] if harvester_settings['set_spec'] != '' else None
        institution_name = harvester_settings['repository_name']

        # Get the collection name. If we hit the OAI-PMH repository, cache the response in memory.
        if base_url in self.oai_pmh_cache:
            if collection_key:
                if collection_key in self.oai_pmh_cache[base_url]:
                    collection_name = self.oai_pmh_cache[base_url][collection_key]
                else:
                    oai_pmh_sets = self.get_oai_pmh_sets(base_url)
                    if collection_key in oai_pmh_sets:
                        collection_name = oai_pmh_sets[collection_key]
                        self.oai_pmh_cache[base_url] = oai_pmh_sets
                    else:
                        raise IndexerError('OAI-PMH repository "{}" does not contain a set with setSpec "{}"'.format(base_url, collection_key))
            else:
                collection_name = collection_key
        else:
            oai_pmh_sets = self.get_oai_pmh_sets(base_url)
            if collection_key:
                if collection_key in oai_pmh_sets:
                    collection_name = oai_pmh_sets[collection_key]
                    self.oai_pmh_cache[base_url] = oai_pmh_sets
                else:
                    raise IndexerError('OAI-PMH repository "{}" does not contain a set with setSpec "{}"'.format(base_url, collection_key))
            else:
                collection_name = collection_key

        if self.args['dry_run']:
            s3_domain_name = 'example.com'
        else:
            s3_domain_name = self.config['s3']['sync']['destination']['domain_name']

        return PRLSolrDocument(
            file_object,
            identifier,
            institution_key,
            institution_name,
            collection_key,
            collection_name,
            self.config['metadata']['dublin_core']['solr_mapping'],
            self.config['metadata']['dublin_core']['thumbnail_field_patterns'],
            s3_domain_name
        )

    def save_thumbnail(self, prl_solr_document: PRLSolrDocument):
        """Puts thumbnail on the local filesystem and on S3."""

        self.upload_thumbnail(
            prl_solr_document,
            self.download_thumbnail(prl_solr_document))
        logging.debug(
            '%s thumbnail saved',
            prl_solr_document.get_record_identifier())

    def download_thumbnail(self, prl_solr_document: PRLSolrDocument):
        """Puts the thumbnail file in its place on the file system, and returns its path."""

        # TODO: need better exception handling here
        # should use the same id for S3 object as is used for Solr document
        record_identifier = prl_solr_document.get_record_identifier()
        try:
            filepath = os.path.join(
                os.path.abspath(os.path.expanduser(self.config['s3']['sync']['source'])),
                'institution_key', # TODO
                'collection_key', # TODO
                prl_solr_document.thumbnail_s3_key + guess_extension(prl_solr_document.original_thumbnail_metadata()['content-type'])
                )
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            n_tries = 3
            for try_i in range(1, n_tries + 1):
                try:
                    response = requests.get(
                        prl_solr_document.original_thumbnail_metadata()['url'],
                        timeout=30,
                        stream=True)
                    # Fail on 4xx or 5xx
                    response.raise_for_status()
                    with open(filepath, 'wb') as image_file:
                        for chunk in response.iter_content(chunk_size=1024):
                            image_file.write(chunk)
                    logging.debug(
                        '%s thumbnail put on local filesystem at %s',
                        record_identifier,
                        filepath)
                    return filepath
                except requests.Timeout as e:
                    if try_i < n_tries:
                        msg = 'Thumbnail download timed out, retrying...'
                        logging.info(msg)
                        # Continue loop
                    else:
                        # No more tries left, so fail
                        msg = 'Failed to download thumbnail after {} tries: {}'.format(n_tries, str(e))
                        raise IndexerError(msg)
                except (requests.RequestException, IOError) as e:
                    msg = 'Failed to download thumbnail: {}'.format(e)
                    raise IndexerError(msg)
        except Exception as e:
            raise IndexerError(
                'Failed to put thumbnail on local filesystem: {}'.format(e))

    def upload_thumbnail(self, prl_solr_document: PRLSolrDocument, filepath: str):
        """Puts the thumbnail on S3."""

        try:
            self.s3.put_object(
                Bucket=self.config['s3']['sync']['destination']['s3_uri'],
                Key=prl_solr_document.thumbnail_s3_key,
                Body=open(filepath, 'rb'),
                ContentType=prl_solr_document.original_thumbnail_metadata()['content-type']
                )
            logging.debug(
                '%s thumbnail put on S3',
                prl_solr_document.get_record_identifier())
        except BotoCoreError as e:
            raise IndexerError('Failed to put thumbnail on S3: {}'.format(e.msg))

    def unsave_thumbnail(self, record_identifier: str):
        """Removes thumbnail from the local filesystem and from S3."""

        try:
            thumbnail_s3_key = urllib.parse.quote(record_identifier, safe='')
            filepath = os.path.join(
                os.path.abspath(os.path.expanduser(
                    self.config['s3']['sync']['source'])),
                'institution_key',
                'collection_key',
                thumbnail_s3_key + guess_extension(self.s3.get_object(
                    Bucket=self.config['s3']['sync']['destination']['s3_uri'],
                    Key=thumbnail_s3_key)['ContentType']))

            os.remove(filepath)
            logging.debug(
                '%s thumbnail removed from local filesystem at %s',
                record_identifier,
                filepath
                )

            # TODO: clean up empty parent directories
            self.s3.delete_object(
                Bucket=self.config['s3']['sync']['destination']['s3_uri'],
                Key=thumbnail_s3_key)
            logging.debug('%s thumbnail removed from S3', record_identifier)
        except BotoCoreError as e:
            raise IndexerError(
                'Failed to remove thumbnail from S3: {}'.format(e.msg)
                )
        except Exception as e:
            raise IndexerError(
                'Failed to remove thumbnail from local filesystem: {}'.format(e)
                )


if __name__ == '__main__':

    # Parse command line arguments.
    parser = argparse.ArgumentParser(
        description='Indexer for Pacific Rim Library back-end.')
    parser.add_argument(
        '-n', '--dry-run',
        action='store_const',
        const=True,
        help='perform a trial run with no changes made')
    parser.add_argument(
        '-t', '--harvest-dir',
        metavar='PATH',
        action='store',
        default=os.getcwd(),
        help='directory to watch for changes '
             '(if unspecified, defaults to current working directory)')
    indexer_args = vars(parser.parse_args())

    config = get_config()
    config_dir = config['dir']

    # Set up logging.
    logging_config_filename = os.path.expanduser(os.path.join(config_dir, config['files']['logging']))
    with open(logging_config_filename, 'r') as logging_config_file:
        logging_config = yaml.load(logging_config_file)
    logging.config.dictConfig(logging_config)

    # Set up the Indexer.
    indexer_config_filename = os.path.expanduser(os.path.join(config_dir, config['files']['app']))
    with open(indexer_config_filename, 'r') as indexer_config_file:
        indexer_config = toml.load(indexer_config_file)
    indexer = Indexer(indexer_args, indexer_config)
    indexer.connect()

    # Watch harvest directory for changes.
    observer = Observer()
    observer.schedule(
        IndexerEventHandler(indexer),
        indexer_args['harvest_dir'],
        recursive=True)

    # Also watch harvester settings directory for changes.
    observer.schedule(
        HarvestSettingsEventHandler(indexer),
        os.path.dirname(indexer.get_harvester_settings_path())
    )
    observer.start()

    try:
        logging.info('Waiting for changes...')
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        # Ctrl-C gets us here.
        logging.info('Keyboard interrupt, exiting...')
    except IndexerError as e:
        logging.critical(e)
        logging.critical('Exiting...')
    except Exception as e:
        logging.critical('Unexpected error: %s', e)
        logging.critical('Exiting...')

    observer.stop()
    observer.join()

    indexer.disconnect()
