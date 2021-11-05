#!/usr/bin/python3

"""Indexer for PRL."""

import argparse
from io import TextIOWrapper
import json
from json import JSONDecodeError
import logging
import logging.config
import os
from queue import Queue
import re
import time
from typing import Any, Dict, List
import urllib

import boto3
from botocore.exceptions import BotoCoreError, ProfileNotFound
from bs4 import BeautifulSoup
import javaobj.v1 as javaobj
import plyvel
from pysolr import Solr
import requests
from sickle import Sickle
import sickle.oaiexceptions
import toml
from watchdog.observers import Observer
import yaml

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
        self.harvester_settings = None
        self.record_sets = None

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
            self.harvester_settings = plyvel.DB(
                os.path.expanduser(self.config['leveldb']['harvester_settings']['path']),
                create_if_missing=True
            )
            self.record_sets = plyvel.DB(
                os.path.expanduser(self.config['leveldb']['record_sets']['path']),
                create_if_missing=True
            )
            self.set_harvester_settings()
        except plyvel.IOError as e:
            raise IndexerError('Failed to instantiate LevelDB instance: {}'.format(repr(e)))

    def _connect_external_services(self):
        """Initializes the interfaces for all third-party services NOT instantiated by this module."""

        try:
            solr_base_url = self.config['solr']['base_url']

            # Make sure we can connect to Solr.
            def solr_ping(base_url):
                """Raises an error if we can't connect to Solr."""
                o = urllib.parse.urlsplit(solr_base_url)
                ping_url = urllib.parse.urlunsplit(o[:2] + (os.path.join(o.path, 'admin/ping'),) + o[3:])
                requests.get(ping_url).raise_for_status()

            solr_ping(solr_base_url)

            self.solr = Solr(solr_base_url, always_commit=True)
            self.s3 = boto3.Session(
                profile_name=self.config['s3']['configure']['profile_name']
            ).client('s3')
        except requests.exceptions.RequestException as e:
            raise IndexerError('Connection failed: {}'.format(e))
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
            self.harvester_settings.close()
            self.record_sets.close()
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
        Returns a relative path with either one or two components.
        
        Intended to be called ONLY on paths representing institution/repository or collection/set directories.
        """
        harvest_dir_prefix = self.config['filesystem']['harvest_dir_prefix']

        return os.path.relpath(path, harvest_dir_prefix)

    def read_harvester_settings_file(self, path: str) -> Dict[str, Dict[str, str]]:
        """Returns a dictionary representing the harvester settings.

        First, tries reading the settings as if the source file is UTF-8 encoded JSON of the following form (used for testing):

        {
            "harvester_settings_key_1": {
                "repository_name": "repository_name_1",
                "base_url": "http://example.edu/oai2",
                "set_spec": "set_spec_1",
                "split_by_set": False
            },
            ...
        }

        If that fails, tries reading the settings as if the source file is a serialized java.util.Hashtable instance from jOAI (used for production).
        """

        try:
            # See if it's in JSON already.
            with open(path, 'r') as harvester_settings_file:
                # Make sure we transform the key before storing.
                return {
                    self.get_harvester_settings_key(key): metadata
                    for key, metadata in json.load(harvester_settings_file).items()
                }
        except JSONDecodeError as e:
            # Invalid JSON.
            raise IndexerError('Cannot load scheduled harvests settings: {}'.format(e))
        except FileNotFoundError as e:
            # This file won't exist when no harvests have been scheduled, so it's probably fine.
            logging.debug('Scheduled harvests settings file does not exist: {}'.format(path))
            return {}
        except UnicodeDecodeError as e:
            logging.debug('Config file is not JSON: {}'.format(e))

            # Open the file in binary mode and try to parse it with javaobj.
            with open(path, 'rb') as harvester_settings_file:
                pobj = javaobj.loads(harvester_settings_file.read())

            scheduled_harvest_class = self.config['leveldb']['harvester_settings']['source']['classes']['scheduled_harvest']
            is_scheduled_harvest = lambda h: scheduled_harvest_class in str(h)

            return {
                self.get_harvester_settings_key(pobj_harvest.harvestDir.path): {
                    'repository_name': pobj_harvest.repositoryName,
                    'base_url': pobj_harvest.baseURL,
                    'set_spec': pobj_harvest.setSpec,
                    'split_by_set': pobj_harvest.splitBySet
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

        harvester_settings_path = self.get_harvester_settings_path()
        new_harvester_settings = self.read_harvester_settings_file(harvester_settings_path)
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
        if not self.args['dry_run']:

            record_metadata = self.get_key_record_metadata(path)
            record_identifier = record_metadata[0]
            record_sets_serialized_encoded = self.record_sets.get(record_identifier.encode())

            # Generate a Solr document from the metadata record.
            with open(path, 'r', encoding='utf-8') as record_file:
                prl_solr_document = self.get_solr_document(record_file)

            # If there is a thumbnail, save it to the system.
            if prl_solr_document.original_thumbnail_metadata():
                thumbnail_saved = self.save_thumbnail(prl_solr_document)
                if not thumbnail_saved:
                    prl_solr_document.discard_incorrect_thumbnail_url()

            record_identifier = prl_solr_document.id

            # Determine whether or not this is a create or an update.
            if record_sets_serialized_encoded is None:
                action = 'create'
            else:
                action = 'update'
                # If we've processed this record in the past, make sure we don't completely overwrite the collectionKey or collectionName fields.
                # We save these locally in LevelDB.
                record_sets = json.loads(record_sets_serialized_encoded.decode())
                prl_solr_document.complete_collection_list(record_sets['collectionKey'], record_sets['collectionName'])

            pysolr_doc = prl_solr_document.get_pysolr_doc()
            collection_key = pysolr_doc['collectionKey']
            collection_name = pysolr_doc['collectionName']

            try:
                self.solr.add([pysolr_doc], overwrite=True)
                logging.debug('%s %sd in Solr', record_identifier, action)

                self.record_sets.put(
                    record_identifier.encode(),
                    json.dumps({'collectionKey': collection_key, 'collectionName': collection_name}).encode()
                    )
                logging.info('%s %sd in PRL', record_identifier, action)
            except plyvel.Error as e:
                self.solr.delete(id=record_identifier)
                raise IndexerError('Failed to PUT on LevelDB: {}'.format(e))
            except Exception as e:
                raise IndexerError('Failed to update Solr document: {}'.format(e))
        else:
            logging.info('DRY-RUN: %s updated in PRL', record_identifier)

    def remove_record(self, path: str):
        """Removes a metadata record from PRL.
        
        Responds to IndexerEventHandler.on_deleted filesystem event.
        """
        if not self.args['dry_run']:
            try:
                record_metadata = self.get_key_record_metadata(path)
                record_identifier = record_metadata[0]
                # We're certain that our serialized JSON is valid.
                record_sets = json.loads(self.record_sets.get(record_identifier.encode()).decode())
            except plyvel.Error as e:
                raise IndexerError('Failed to GET on LevelDB: {}'.format(e))

            # Either remove the record from the system, or update it.
            if len(record_sets['collectionKey']) == 1:
                # Remove the thumbnail if there is one.
                try:
                    pysolr_doc = self.solr.search('id:"{0}"'.format(record_identifier)).docs[0]
                except Exception as e:
                    raise IndexerError('Failed to GET {} from Solr: {}'.format(record_identifier, e))
                if 'thumbnail_url' in pysolr_doc:
                    self.unsave_thumbnail(pysolr_doc['thumbnail_url'], record_identifier)

                # Remove the document from Solr.
                try:
                    self.solr.delete(id=record_identifier)
                except Exception as e:
                    raise IndexerError('Failed to DELETE {} from Solr: {}'.format(record_identifier, e))
                logging.debug('%s removed from Solr', record_identifier)

                try:
                    self.record_sets.delete(record_identifier.encode())
                except plyvel.Error as e:
                    raise IndexerError('Failed to DELETE on LevelDB: {}'.format(e))

                logging.info('%s removed from PRL', record_identifier)
            else:
                # Update the list of collections that the record belongs to.
                # This is the case when a record belongs to more than one OAI-PMH set.
                collection_key = list(filter(lambda x: x != record_metadata[3], record_sets['collectionKey']))
                collection_name = list(filter(lambda x: x != record_metadata[4], record_sets['collectionName']))

                pysolr_doc = {
                    'id': record_identifier,
                    'collectionKey': collection_key,
                    'collectionName': collection_name
                }

                try:
                    self.solr.add(
                        [pysolr_doc],
                        fieldUpdates={
                            'collectionKey': 'set',
                            'collectionName': 'set'
                        },
                        overwrite=True
                        )
                except Exception as e:
                    raise IndexerError('Failed to POST {} on Solr: {}'.format(record_identifier, e))
                logging.debug('%s updated in Solr (removed from collection %s)', record_identifier, record_metadata[3])

                try:
                    self.record_sets.put(
                        record_identifier.encode(),
                        json.dumps({'collectionKey': collection_key, 'collectionName': collection_name}).encode()
                        )
                except plyvel.Error as e:
                    raise IndexerError('Failed to PUT on LevelDB: {}'.format(e))

                logging.info('%s updated in PRL (removed from collection %s)', record_identifier, record_metadata[3])
        else:
            logging.info('DRY-RUN: Removed %s', path)

    def get_oai_pmh_metadata(self, base_url: str) -> Dict[str, str]:
        """Returns a dictionary containing top-level metadata and set metadata of an OAI-PMH repository."""

        logging.debug('Retrieving repository and set metadata from OAI-PMH repository %s', base_url)
        try:
            metadata = {}

            # All repositories should have this metadata.
            repository_metadata = Sickle(base_url, timeout=60).Identify()
            if hasattr(repository_metadata, 'repositoryIdentifier'):
                metadata['repository_identifier'] = repository_metadata.repositoryIdentifier
            if hasattr(repository_metadata, 'repositoryName'):
                metadata['repository_name'] = repository_metadata.repositoryName

            # Not all repositories will support sets.
            try:
                set_metadata = Sickle(base_url, timeout=60).ListSets()
                metadata.update({
                    'sets': {
                        s.setSpec: s.setName
                        for s in list(set_metadata)
                    }
                })
            except sickle.oaiexceptions.NoSetHierarchy as e:
                logging.debug('Failed to list sets from OAI-PMH repository %s: %s', base_url, e)

            return metadata

        except requests.RequestException as e:
            raise IndexerError('Failed to get repository metadata from OAI-PMH repository {}: {}'.format(base_url, e))

    def get_solr_document(self, file_object: TextIOWrapper) -> PRLSolrDocument:
        """Builds a Solr document for PRL."""
        identifier, institution_key, institution_name, collection_key, collection_name = self.get_key_record_metadata(file_object.name)

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
            self.config['metadata']['dublin_core']['external_link_field_patterns'],
            self.config['metadata']['dublin_core']['thumbnail_field_patterns'],
            s3_domain_name
        )

    def get_key_record_metadata(self, file_path: str):
        """Determines collection and institution metadata from the filepath of the record.

        Returns a 5-tuple containing the following elements:
            - an identifier for the record
            - an identifier for the institution
            - a human-readable string for the institution
            - an identifier for the collection
            - a human-readable string for the collection

        Side effects:
            - updates local LevelDB cache with OAI-PMH repository metadata
        """

        # ---------------------------------------- #
        # --- Gather all the data we can find. --- #
        # ---------------------------------------- #

        # Get the record identifier from the filename.
        identifier = urllib.parse.unquote(os.path.splitext(os.path.basename(file_path))[0])

        try:
            # The harvester settings will tell us how to get the other metadata.
            harvester_settings_key = None

            potential_harvester_settings_keys = map(self.get_harvester_settings_key,
                                                    [os.path.dirname(file_path), os.path.dirname(os.path.dirname(file_path))])
            # Keep track of keys that we tried, but failed.
            tried_keys = []

            for potential_harvester_settings_key in potential_harvester_settings_keys:
                potential_harvester_settings_serialized_encoded = self.harvester_settings.get(potential_harvester_settings_key.encode())

                if potential_harvester_settings_serialized_encoded:
                    # Found it!
                    harvester_settings_key = potential_harvester_settings_key
                    break
                else:
                    tried_keys.append(potential_harvester_settings_key)

            if harvester_settings_key is not None:
                harvester_settings_serialized_encoded = potential_harvester_settings_serialized_encoded
                harvester_settings_serialized = harvester_settings_serialized_encoded.decode()
                harvester_settings = json.loads(harvester_settings_serialized)
            else:
                # This should never happen. Harvester settings should represent all harvested files.
                raise IndexerError('Cannot find harvester settings in LevelDB for {}'.format(tried_keys))

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
        institution_name = harvester_settings['repository_name']
        set_spec = harvester_settings['set_spec']
        split_by_set = harvester_settings['split_by_set']

        # Fetch repository metadata, and write to the in-memory cache if necessary.
        if base_url in self.oai_pmh_cache:
            oai_pmh_metadata = self.oai_pmh_cache[base_url]
        else:
            oai_pmh_metadata = self.get_oai_pmh_metadata(base_url)
            self.oai_pmh_cache[base_url] = oai_pmh_metadata

        # ----------------------------------------- #
        # --- Determine which values to return. --- #
        # ----------------------------------------- #

        # This is the most common case: an institution specifies a specific set for us to harvest.
        individual_set_harvest = set_spec != '' and not split_by_set

        # This is the case when an institution wants us to harvest all sets from their repository.
        full_repository_harvest = set_spec == '' and split_by_set

        # This is the case when an institution wants us to treat their entire repository as a PRL "collection".
        single_collection_repository = set_spec == '' and not split_by_set

        # Set the return values.
        if individual_set_harvest:
            institution_key = os.path.dirname(harvester_settings_key)
            collection_key = set_spec
            collection_name = oai_pmh_metadata['sets'][set_spec]

        elif full_repository_harvest:
            institution_key = harvester_settings_key
            collection_key = os.path.basename(os.path.dirname(file_path))
            collection_name = oai_pmh_metadata['sets'][set_spec]

        elif single_collection_repository:
            institution_key = os.path.dirname(harvester_settings_key)
            collection_key = os.path.basename(harvester_settings_key)
            collection_name = oai_pmh_metadata['repository_name']
        else:
            raise IndexerError('Unable to handle harvest configuration: {}'.format(harvester_settings_key))


        return (identifier, institution_key, institution_name, collection_key, collection_name)

    def save_thumbnail(self, prl_solr_document: PRLSolrDocument):
        """Puts thumbnail on the local filesystem and on S3.

        Returns the Boolean value of whether or not a thumbnail was saved."""

        thumbnail_path = self.download_thumbnail(prl_solr_document)
        if thumbnail_path:
            self.upload_thumbnail(prl_solr_document, thumbnail_path)
            logging.debug(
                '%s thumbnail saved',
                prl_solr_document.get_record_identifier())
            return True
        else:
            return False

    def download_thumbnail(self, prl_solr_document: PRLSolrDocument):
        """Puts the thumbnail file in its place on the file system.

        Returns its path, or None if no thumbnail could be fetched."""

        # TODO: need better exception handling here
        thumbnail_s3_key = prl_solr_document.get_thumbnail_s3_key()
        try:
            filepath = os.path.join(
                os.path.abspath(os.path.expanduser(self.config['s3']['sync']['source'])),
                thumbnail_s3_key
                )
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            original_thumbnail_url = prl_solr_document.original_thumbnail_metadata()['url']
            n_tries = 3
            for try_i in range(1, n_tries + 1):
                try:
                    response = requests.get(original_thumbnail_url, timeout=30, stream=True)
                    # Fail on 4xx or 5xx
                    response.raise_for_status()
                    # Make sure the Content-Type is what we expect. Some servers discriminate against robots.
                    if re.match(re.compile('image/.+'), response.headers.get('Content-Type')):
                        with open(filepath, 'wb') as image_file:
                            for chunk in response.iter_content(chunk_size=1024):
                                image_file.write(chunk)
                        logging.debug(
                            '%s thumbnail put on local filesystem at %s',
                            thumbnail_s3_key,
                            filepath)
                        return filepath
                    else:
                        logging.debug('Robots cannot access %s', original_thumbnail_url)
                        return None
                except requests.Timeout as e:
                    if try_i < n_tries:
                        msg = 'Thumbnail download timed out, retrying...'
                        logging.info(msg)
                        # Continue loop
                    else:
                        # No more tries left, so fail
                        msg = 'Failed to download thumbnail after {} tries: {}'.format(n_tries, str(e))
                        logging.debug(msg)
                        return None
                except (requests.RequestException, IOError) as e:
                    msg = 'Failed to download thumbnail: {}'.format(e)
                    logging.debug(msg)
                    return None
        except Exception as e:
            raise IndexerError(
                'Failed to put thumbnail on local filesystem: {}'.format(e))

    def upload_thumbnail(self, prl_solr_document: PRLSolrDocument, filepath: str):
        """Puts the thumbnail on S3."""

        try:
            self.s3.put_object(
                Bucket=self.config['s3']['sync']['destination']['s3_uri'],
                Key=prl_solr_document.get_thumbnail_s3_key(),
                Body=open(filepath, 'rb'),
                ContentType=prl_solr_document.original_thumbnail_metadata()['content-type']
                )
            logging.debug(
                '%s thumbnail put on S3',
                prl_solr_document.get_record_identifier())
        except BotoCoreError as e:
            raise IndexerError('Failed to put thumbnail on S3: {}'.format(e.msg))

    def unsave_thumbnail(self, thumbnail_url: str, record_identifier: str):
        """Removes thumbnail from the local filesystem and from S3."""

        try:
            thumbnail_s3_key = os.path.relpath(urllib.parse.urlparse(urllib.parse.unquote(thumbnail_url)).path, '/')
            filepath = os.path.join(
                    os.path.abspath(os.path.expanduser(self.config['s3']['sync']['source'])),
                    thumbnail_s3_key
                    )
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
        logging_config = yaml.load(logging_config_file, Loader=yaml.FullLoader)
    logging.config.dictConfig(logging_config)

    # Set up the Indexer.
    indexer_config_filename = os.path.expanduser(os.path.join(config_dir, config['files']['app']))
    with open(indexer_config_filename, 'r') as indexer_config_file:
        indexer_config = toml.load(indexer_config_file)
    indexer = Indexer(indexer_args, indexer_config)
    indexer.connect()

    # Queue for exceptions, shared across all threads.
    exceptions_queue = Queue()

    # Watch harvest directory for changes.
    harvest_observer = Observer()
    harvest_observer.schedule(
        IndexerEventHandler(
            indexer,
            exceptions_queue,
            ignore_patterns=['*/.*/*'],
            ignore_directories=True
        ),
        indexer_args['harvest_dir'],
        recursive=True)
    harvest_observer.start()

    # Also watch harvester settings directory for changes.
    harvestersettings_observer = Observer()
    harvestersettings_observer.schedule(
        HarvestSettingsEventHandler(
            indexer,
            exceptions_queue,
            ignore_directories=True
        ),
        indexer.config['leveldb']['harvester_settings']['source']['base_path']
    )
    harvestersettings_observer.start()

    # Wait for the watcher threads to report any exceptions, and exit on the first one.
    try:
        logging.info('Waiting for changes...')
        while exceptions_queue.empty():
            time.sleep(1)
        raise exceptions_queue.get()
    except KeyboardInterrupt:
        # Ctrl-C gets us here.
        logging.info('Keyboard interrupt')
    except IndexerError as e:
        logging.critical(e, exc_info=True)
    except Exception as e:
        logging.critical('Unexpected error: %s', e, exc_info=True)
    finally:
        logging.info('Exiting...')

        harvest_observer.stop()
        harvest_observer.join()

        harvestersettings_observer.stop()
        harvestersettings_observer.join()

        indexer.disconnect()

        logging.info('Done.')
