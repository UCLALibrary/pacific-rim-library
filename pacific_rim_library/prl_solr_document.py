"""Generates PRL Solr documents."""

import collections
from io import TextIOWrapper
import logging
import logging.config
from mimetypes import guess_type, guess_extension
import os
import re
from typing import Dict, List
import urllib

from bs4 import BeautifulSoup
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
import requests

from pacific_rim_library.date_cleaner_and_faceter import DateCleanerAndFaceter
from pacific_rim_library.hyperlink_relevance_heuristic_sorter import HyperlinkRelevanceHeuristicSorter


# Mapping from Dublin Core field names to Solr field names.
SOLR_FIELD_MAP = {
    "title": "title_keyword",
    "creator": "creator_keyword",
    "subject": "subject_keyword",
    "description": "description_keyword",
    "publisher": "publisher_keyword",
    "contributor": "contributor_keyword",
    "date": "date_keyword",
    "type": "type_keyword",
    "format": "format_keyword",
    "identifier": "identifier_keyword",
    "source": "source_keyword",
    "language": "language_keyword",
    "relation": "relation_keyword",
    "coverage": "coverage_keyword",
    "rights": "rights_keyword"
}

# Regular expressions to match against fields to search for external link URLs.
EXTERNAL_LINK_FIELD_PATTERNS = [
  "identifier",
  "identifier\\.(?:.+)",
]

# Regular expressions to match against fields to search for thumbnail URLs.
THUMBNAIL_FIELD_PATTERNS = [
  "description",
  "identifier\\.thumbnail",
  "identifier",
  "identifier\\.(?:.+)",
]

class PRLSolrDocument:
    """Generates PRL Solr documents."""

    def __init__(self, file_object: TextIOWrapper, identifier: str,
                 institution_key: str, institution_name: str,
                 collection_key: str, collection_name: str, s3_host: str):
        """Generates a PRL Solr document."""

        self.s3_host = s3_host

        self.soup = BeautifulSoup(file_object, 'lxml-xml', from_encoding='utf-8')
        self.id = identifier
        self.pysolr_doc = {}
        self.pysolr_doc.update({
            'id': self.id,
            'collectionKey': [collection_key],
            'collectionName': [collection_name],
            'institutionKey': institution_key,
            'institutionName': institution_name
        })
        self._add_fields()
        self._add_decades()
        self._add_external_links()
        self.original_thumbnail_metadata_prop = self._find_potential_thumbnail()

    def get_record_identifier(self):
        return self.id

    def original_thumbnail_metadata(self):
        return self.original_thumbnail_metadata_prop

    def complete_collection_list(self, collection_key_list: List[str], collection_name_list: List[str]):
        """Completes the list of collections for this record.

        Modifies the values under 'collectionKey' and 'collectionName' if the
        local 'record_sets' LevelDB instance contains other sets (which implies
        that those sets were saved in the previous version of this record's
        Solr document).
        """
        current_collection_key_list = self.pysolr_doc['collectionKey']
        current_collection_name_list = self.pysolr_doc['collectionName']

        if current_collection_key_list[0] not in collection_key_list:
            self.pysolr_doc['collectionKey'] = collection_key_list + current_collection_key_list
            self.pysolr_doc['collectionName'] = collection_name_list + current_collection_name_list
        else:
            self.pysolr_doc['collectionKey'] = collection_key_list
            self.pysolr_doc['collectionName'] = collection_name_list

    def get_pysolr_doc(self):
        return self.pysolr_doc

    # TODO: rename this method, or replace with a library
    def _add_value_possibly_duplicate_key(self, key, value, dic):
        """Adds a key-value pair to a dictionary that may already have a value for that key. If that's the case, put both values into a list.
        This is how pysolr wants us to represent duplicate fields.
        """
        if key in dic:
            if isinstance(dic[key], collections.MutableSequence):
                dic[key].append(value)
            else:
                dic[key] = [dic[key], value]
        else:
            dic[key] = value

    def _add_fields(self):
        doc = self.get_pysolr_doc()

        for tag in self.soup.find('dc').contents:
            # add to doc

            # ignore newlines and other whitespace in the list of tags
            if tag.name is None:
                continue

            try:
                # only process Dublin Core fields (no qualified DC)
                name = SOLR_FIELD_MAP[tag.name]
            except KeyError as e:
                continue
            else:
                value = tag.string
                if value is not None:
                    self._add_value_possibly_duplicate_key(name, value, doc)
                    if name == SOLR_FIELD_MAP['title'] and 'first_title' not in doc:
                        doc['first_title'] = value

    def _add_decades(self):
        doc = self.get_pysolr_doc()

        years = set()

        for tag in self.soup.find('dc').find_all('date'):
            value = tag.string
            if tag.name == 'date' and value is not None:
                # build up a set of all the years included in the metadata
                years.add(value)

        if years:
            decades = DateCleanerAndFaceter().decades(years)

            if decades:
                for decade in decades:
                    self._add_value_possibly_duplicate_key('decade', decade, doc)
                doc['sort_decade'] = min(decades, key=lambda x: int(x))
            else:
                logging.debug('Failed to represent "%s" as a list of decades', str(years))

    def _add_external_links(self):
        doc = self.get_pysolr_doc()

        # TODO: change to set
        hyperlinks = []

        for bs_filter in EXTERNAL_LINK_FIELD_PATTERNS:
            for tag in self.soup.find('dc').find_all(re.compile(bs_filter)):
                value = tag.string
                if value is not None:
                    try:
                        URLValidator()(value)
                        if os.path.splitext(urllib.parse.urlparse(value).path)[1] not in ['.jpg', '.jpeg', '.png', '.tif', '.tiff']:
                            hyperlinks.append(value)
                    except ValidationError:
                        # Continue loop
                        pass

        if hyperlinks:
            identifier = self.get_record_identifier()
            if self._is_oai_identifier(identifier):
                ident = identifier.split(sep=':', maxsplit=2)[2]
            else:
                ident = identifier

            heuristics = {
                'host': self.pysolr_doc['institutionKey'],
                'identifier': ident
            }
            hrhs = HyperlinkRelevanceHeuristicSorter()
            sorted_links = hrhs.sort(heuristics, hyperlinks)
            doc['external_link'] = sorted_links[0]

            rest = sorted_links[1:]
            if rest:
                doc['alternate_external_link'] = rest

    def add_thumbnail_url(self):
        """If a thumbnail has been identified for this item, adds its URL to the Solr document."""
        if self.original_thumbnail_metadata_prop is not None:
            urlencoded_thumbnail_path = urllib.parse.quote(self.get_thumbnail_s3_key())
            self.get_pysolr_doc()['thumbnail_url'] = urllib.parse.urlunparse(('http', self.s3_host, urlencoded_thumbnail_path, '', '', ''))

    def has_thumbnail_format(self):
        """Returns true if the thumbnail format is known, and false otherwise."""
        return (self.original_thumbnail_metadata_prop['extension'] is not None and
            self.original_thumbnail_metadata_prop['content-type'] is not None)

    def set_thumbnail_format(self, content_type):
        """Sets the format metadata for the thumbnail."""

        # Determine the filetype extension based on the content-type
        filetype_extension = guess_extension(content_type)
        if filetype_extension == '.jpe':
            # Use the more common .jpg extension instead of the default .jpe
            filetype_extension = '.jpg'

        self.original_thumbnail_metadata_prop['extension'] = filetype_extension
        self.original_thumbnail_metadata_prop['content-type'] = content_type

    def _find_potential_thumbnail(self):
        """Return the URL (and image format metadata, if it can be inferred from the URL) of a potential thumbnail
        found in a Dublin Core record. If none exists, return None.
        """
        checked_urls = []
        for bs_filter in THUMBNAIL_FIELD_PATTERNS:
            # search for tags that match the filter (can be regex or string, see )
            tags = self.soup.find_all(re.compile(bs_filter))

            for tag in tags:
                value = tag.string
                if value is not None:
                    possible_url = tag.string
                    try:
                        URLValidator()(possible_url)
                        if possible_url not in checked_urls:
                            # Try to guess the media type
                            guessed_type = guess_type(possible_url)[0]
                            if guessed_type:
                                try:
                                    mime_type_match = re.search(re.compile('image/.+'), guessed_type)
                                    if mime_type_match is not None:
                                        logging.debug('Found image at {}'.format(possible_url))
                                        return {
                                            'url': possible_url,
                                            'extension': os.path.splitext(urllib.parse.urlparse(possible_url).path)[1],
                                            'content-type': guessed_type
                                        }
                                    else:
                                        checked_urls.append(possible_url)
                                # no content-type
                                except KeyError:
                                    checked_urls.append(possible_url)
                            else:
                                if "file=thumbnail" in urllib.parse.urlparse(possible_url).query:
                                    logging.debug('Found image at {}'.format(possible_url))

                                    # Cannot infer image format metadata from the URL, so will determine it when the
                                    # Indexer saves it to disk
                                    return {
                                        'url': possible_url,
                                        'extension': None,
                                        'content-type': None
                                    }
                    except ValidationError:
                        # Continue loop
                        pass
        return None

    def _make_thumbnail_request(self, fn, url, stream, redirect):
        """Make request to the given URL and handle the response.

        If we can do something with the response, return it, otherwise return None.
        """
        n_tries = 0
        max_tries = 3
        while n_tries < max_tries:
            try:
                r = fn(url, stream=stream, timeout=60, allow_redirects=redirect)
                r.raise_for_status()
                break
            except requests.Timeout:
                # try a couple more times, server may be restarting
                logging.debug('Network timeout while trying to fetch image at {}, trying again...')
                n_tries += 1
            except (requests.ConnectionError, requests.TooManyRedirects, requests.URLRequired, requests.HTTPError, requests.RequestException):
                return None

        if n_tries == max_tries:
            logging.debug('No more tries left, moving on')
            return None
        else:
            return r

    def _is_oai_identifier(self, identifier: str) -> bool:
        """Return true if the given identifier follows the syntax specified here:

        http://www.openarchives.org/OAI/2.0/guidelines-oai-identifier.htm
        """
        components = identifier.split(sep=':', maxsplit=2)
        return components[0] == 'oai' and len(components) == 3

    def get_thumbnail_s3_key(self):
        """Returns the AWS S3 key to use for the thumbnail."""

        return PRLSolrDocument.create_thumbnail_s3_key(
            self.pysolr_doc['institutionKey'],
            self.pysolr_doc['collectionKey'][0],
            self.get_record_identifier(),
            self.original_thumbnail_metadata()['extension'] if self.has_thumbnail_format() else ''
        )

    @staticmethod
    def create_thumbnail_s3_key(institution_key: str, collection_key: str, identifier: str, extension: str):
        """Creates a AWS S3 key for a thumbnail using its filetype and various identifiers."""
        return '{}/{}/{}{}'.format(
            urllib.parse.quote(institution_key, safe=''),
            urllib.parse.quote(collection_key, safe=''),
            urllib.parse.quote(identifier, safe=''),
            extension
        )