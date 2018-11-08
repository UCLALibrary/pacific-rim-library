"""Generates PRL Solr documents."""

import collections
from io import TextIOWrapper
import logging
import logging.config
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


class PRLSolrDocument:
    """Generates PRL Solr documents."""

    def __init__(self, file_object: TextIOWrapper, identifier: str,
                 institution_key: str, institution_name: str,
                 collection_key: str, collection_name: str, field_map: Dict[str, str],
                 thumbnail_field_patterns: List[str], s3_host: str):
        """Generates a PRL Solr document."""

        self.field_map = field_map
        self.thumbnail_field_patterns = thumbnail_field_patterns
        self.s3_host = s3_host
        self.original_thumbnail_metadata_prop = None

        self.soup = BeautifulSoup(file_object, 'lxml-xml')
        self.id = identifier
        self.pysolr_doc = {}
        self.pysolr_doc.update({
            'id': self.id,
            'collectionKey': collection_key,
            'collectionName': collection_name,
            'institutionKey': institution_key,
            'institutionName': institution_name
        })
        self._add_fields()
        self._add_decades()
        self._add_external_links()
        self._add_thumbnail_url()

    def get_record_identifier(self):
        return self.id

    def original_thumbnail_metadata(self):
        return self.original_thumbnail_metadata_prop

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

        if self.field_map is not None:
            for tag in self.soup.find('dc').contents:
                # add to doc

                # ignore newlines and other whitespace in the list of tags
                if tag.name is None:
                    continue

                try:
                    # only process Dublin Core fields (no qualified DC)
                    name = self.field_map[tag.name]
                except KeyError as e:
                    continue
                else:
                    value = tag.string
                    if value is not None:
                        self._add_value_possibly_duplicate_key(name, value, doc)
                        if name == self.field_map['title'] and 'first_title' not in doc:
                            doc['first_title'] = value

    def _add_decades(self):
        doc = self.get_pysolr_doc()

        years = set()

        if self.field_map is not None:
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
                logging.debug('years "{}" -> decades "{}"'.format(years, decades))
            else:
                logging.error('Failed to represent %s as decades', str(years))

    def _add_external_links(self):
        doc = self.get_pysolr_doc()

        # TODO: change to set
        hyperlinks = []

        if self.field_map is not None:
            for tag in self.soup.find('dc').find_all('identifier'):
                value = tag.string
                if value is not None:
                    logging.debug(tag.name + ' ' + value)
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
                'host': 'http://example.edu',
                'identifier': ident
            }
            hrhs = HyperlinkRelevanceHeuristicSorter()
            sorted_links = hrhs.sort(heuristics, hyperlinks)
            doc['external_link'] = sorted_links[0]

            rest = sorted_links[1:]
            if rest:
                doc['alternate_external_link'] = rest

    def _add_thumbnail_url(self):
        doc = self.get_pysolr_doc()

        original_thumbnail_metadata_prop = self._find_thumbnail()
        if original_thumbnail_metadata_prop is not None:
            self.original_thumbnail_metadata_prop = original_thumbnail_metadata_prop
            self.thumbnail_s3_key = urllib.parse.quote(self.get_record_identifier(), safe='')
            doc['thumbnail_url'] = urllib.parse.urlunparse(('http', self.s3_host, urllib.parse.quote(self.thumbnail_s3_key, safe=''), '', '', ''))
    
    def _find_thumbnail(self):
        """Return the URL and Content-Type of the thumbnail for a Dublin Core record.
        If none exists, return None.
        """
        checked_urls = []
        for f in self.thumbnail_field_patterns:
            # search for tags that match the filter (can be regex or string, see )
            tags = self.soup.find_all(f)

            for tag in tags:
                value = tag.string
                if value is not None:
                    possible_url = tag.string
                    try:
                        URLValidator()(possible_url)
                        if possible_url not in checked_urls:
                            logging.debug('Checking for thumbnail at {}'.format(possible_url))
                            # TODO: maybe check path extension before doing get request?
                            #r = requests.get(possibleUrl)

                            resp = self._make_thumbnail_request(requests.head, possible_url, False, True)

                            if resp is not None:
                                try:
                                    m = re.search(re.compile('image/(?:jpeg|tiff|png)'), resp.headers['content-type'])
                                    logging.debug('Match: {}'.format(m))
                                    if m is not None:
                                        return {
                                            'url': resp.url,
                                            'content-type': resp.headers['content-type']
                                        }
                                    else:
                                        checked_urls.append(possible_url)
                                # no content-type
                                except KeyError:
                                    checked_urls.append(possible_url)
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
                logging.debug('Trying again...')
                n_tries += 1
            except (requests.ConnectionError, requests.TooManyRedirects, requests.URLRequired, requests.HTTPError, requests.RequestException):
                return None

        if n_tries == max_tries:
            logging.debug('Network timeout: {}'.format(url))
            return None
        else:
            return r

    def _is_oai_identifier(self, identifier: str) -> bool:
        """Return true if the given identifier follows the syntax specified here:

        http://www.openarchives.org/OAI/2.0/guidelines-oai-identifier.htm
        """
        components = identifier.split(sep=':', maxsplit=2)
        return components[0] == 'oai' and len(components) == 3
