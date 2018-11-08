import logging
import logging.config
import urllib.parse


class HyperlinkRelevanceHeuristicSorter:
    """
    Sorts a list of hyperlinks in order of decreasing relevance based on the scoring heuristic.
    """

    def __init__(self):
        pass

    def sort(self, heuristics, links):
        """Sort links based on a heuristic for relevance.

        heuristics - a dictionary consisting of the following keys:
        - "host": a string retrieved from the netloc property of the return value of urllib.parse.urlparse
        - "identifier": the local identifier portion of the OAI identifier as described in http://www.openarchives.org/OAI/2.0/guidelines-oai-identifier.htm if the identifier is structured that way, otherwise the entire OAI identifier
        links - a list of HTTP URLs
        """
        scores = {}
        for link in links:
            scores[link] = self._score(heuristics, link)

        links.sort(key=lambda x: scores[x], reverse=True)
        return links

    def _score(self, heuristics, link):
        """Highest scoring links are most relevant."""

        score = 0
        if heuristics['identifier'] in link:
            score += 1
        netloc = urllib.parse.urlparse(link).netloc
        if heuristics['host'] == netloc:
            score += 1
        return score
