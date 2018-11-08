from dateutil.parser import parse
import logging
import logging.config
import re


class DateCleanerAndFaceter:
    """Class for cleaning and creating decade or year facets for dates."""

    def __init__(self):
        """Initialize the object for use.


        """

        # regular expressions used for matching non-standard date formats
        # TODO: move to separate file
        self.regexes = {
            'match': {},
            'substitution': {},
            'capture': {}
            }

        # years before 0
        self.regexes['match']['suffix-bce'] = r'BC|B\.C\.|BCE|B\.C\.E\.'

        # years after 0
        self.regexes['match']['suffix-ce'] = r'AD|A\.D\.|CE|C\.E\.'

        # a suffix may indicate years before 0 or years after 0
        self.regexes['match']['suffix'] = r'(?:{}|{})'.format(
            self.regexes['match']['suffix-bce'],
            self.regexes['match']['suffix-ce'])

        # two-digit representation of a month: 01 - 12
        self.regexes['match']['mm'] = r'(?:0[1-9]|1[0-2])'

        # two-digit representation of a day of a month: 01 - 31
        self.regexes['match']['dd'] = r'(?:0[1-9]|[1-2]\d|3[0-1])'

        # three-character representation of a month
        self.regexes['match']['mon'] = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'

        # time: e.g., 02:00 am, 12:55 P.M., 3:40
        self.regexes['match']['time'] = r'\d{1,2}[.:]\d{2}(?:[apAP]\.?[mM]\.?)?'

        # require a 1 or 2 digit year to have a suffix
        self.regexes['match']['year0,1'] = r'[1-9]\d{{0,1}}'
        self.regexes['match']['year0,1-plus-suffix'] = r'{} {}'.format(
            self.regexes['match']['year0,1'],
            self.regexes['match']['suffix'])

        # 3 or 4 digit years may or may not have a suffix
        self.regexes['match']['year2,3'] = r'[1-9]\d{{2,3}}'.format(self.regexes['match']['suffix'])
        self.regexes['match']['year2,3-plus-suffix'] = r'{}(?: {})?'.format(
            self.regexes['match']['year2,3'],
            self.regexes['match']['suffix'])

        # a year can have 1, 2, 3, or 4 digits, and may or may not have a suffix according to the above
        self.regexes['match']['year'] = r'(?:{}|{})'.format(
            self.regexes['match']['year0,1-plus-suffix'],
            self.regexes['match']['year2,3-plus-suffix'])

        #
        # year ranges
        #

        # parsing them is complicated, so we'll have a subset of special rules for them
        # we want to capture certain aspects of the year range
        self.regexes['capture']['year0,1-plus-suffix'] = r'({}) ({})'.format(
            self.regexes['match']['year0,1'],
            self.regexes['match']['suffix'])

        # 3 or 4 digit years may or may not have a suffix
        self.regexes['capture']['year2,3-plus-suffix'] = r'({})(?: ({}))?'.format(
            self.regexes['match']['year2,3'],
            self.regexes['match']['suffix'])

        # a year can have 1, 2, 3, or 4 digits, and may or may not have a suffix according to the above
        # 1: 1-2 digit year
        # 2: suffix
        # 3: 3-4 digit year
        # 4: suffix
        self.regexes['capture']['year'] = r'(?:{}|{})'.format(
            self.regexes['capture']['year0,1-plus-suffix'],
            self.regexes['capture']['year2,3-plus-suffix'])

        # sometimes metadata indicates uncertainty about a year, either with a question mark at the end or some other character in place of the one's digit
        self.regexes['match']['year?'] = r'[1-9]\d{1,2}\d?[-*?]'

        # matches a year followed by a 2 digit month (must not be followed by another digit), the year can have a mystery one's place
        # assume that if a month is given, there's no suffix
        self.regexes['match']['year-mm'] = r'{}(?:(?:[-/]{})|[-*?])?(?=\D|$)'.format(
            self.regexes['match']['year'],
            self.regexes['match']['mm'])

        # matches a range of years, separated by either - or /
        self.regexes['match']['year-year'] = r'{}\s*[-/]\s*{}'.format(
            self.regexes['match']['year-mm'],
            self.regexes['match']['year-mm'])

        self.regexes['match']['dd-mon-year-time'] = r'{}\s+{}\s+{}(?:\.\s+{})?'.format(
            self.regexes['match']['dd'],
            self.regexes['match']['mon'],
            self.regexes['match']['year'],
            self.regexes['match']['time'])

        # matches a century string
        self.regexes['match']['century'] = r'(?:1st|2nd|3rd|(?:[4-9]|1[0-9]|20)th)\s+[cC](?:entury)?'
        self.regexes['match']['century-plus-suffix'] = r'{}(?:\s+{})?'.format(
            self.regexes['match']['century'],
            self.regexes['match']['suffix'])

        # order of alternate patterns is important
        self.regexes['match']['date'] = r'(?:({})|({})|({})|({})|({}))'.format(
            self.regexes['match']['century-plus-suffix'],
            self.regexes['match']['year-year'],
            self.regexes['match']['dd-mon-year-time'],
            self.regexes['match']['year?'],
            self.regexes['match']['year'])

        # split the year range in half
        self.regexes['substitution']['year-year-splitter'] = r'({})\s*[-/]\s*({})'.format(
            self.regexes['match']['year-mm'],
            self.regexes['match']['year-mm'])

        self.regexes['substitution']['dd-mon-year-time'] = r'{}\s+{}\s+({})(?:\.\s+{})?'.format(
            self.regexes['match']['dd'],
            self.regexes['match']['mon'],
            self.regexes['match']['year'],
            self.regexes['match']['time'])

        # capture century info
        self.regexes['capture']['century-plus-suffix'] = r'({})(?:\s+({}))?'.format(
            self.regexes['match']['century'],
            self.regexes['match']['suffix'])

    def decades(self, data, disjoint=True):
        """Returns a set of decades that covers all of the years and year ranges in the data.

        data - can be a single string or a set of strings
        disjoint - whether or not to exclude decades in the interim between the earliest and latest decades
        """

        decade_set = set()
        try:
            # multi valued
            assert isinstance(data, set)
            for datum in data:
                preprocessed_year_data = self._extract_year_data(datum)
                decade_set = decade_set | self._enumerate_decades(preprocessed_year_data, disjoint)
        except (AssertionError, TypeError):
            # single value
            preprocessed_year_data = self._extract_year_data(data)
            decade_set = decade_set | self._enumerate_decades(preprocessed_year_data, disjoint)

        return decade_set

    def years(self, data, disjoint=True):
        """Returns a set of years that covers all of the years and year ranges in the data.

        data - can be a single string or a set of strings
        disjoint - whether or not to exclude years in the interim between the earliest and latest years
        """

        year_set = set()
        try:
            # multi valued
            assert isinstance(data, set)
            for datum in data:
                preprocessed_year_data = self._extract_year_data(datum)
                year_set = year_set | self._enumerate_years(preprocessed_year_data, disjoint)

        except TypeError:
            # single value
            preprocessed_year_data = self._extract_year_data(data)
            year_set = self._enumerate_years(preprocessed_year_data, disjoint)

        return year_set

    def _date_match_to_int_or_tuple(self, m):
        """Maps a match of regexes['match']['date'] to a tuple of years, or a single year.

        m - the re.match object

        Match indices:
            0 -> 'century'
            1 -> 'year-year'
            2 -> 'dd-mon-year-time'
            3 -> 'year?'
            4 -> 'year'
        """

        years = set()
        try:
            if m[0] != '':
                # year-range derived from a century
                century = int(re.match(re.compile('\\d+'), m[0]).group(0))

                match = re.compile(self.regexes['capture']['century-plus-suffix']).match(m[0])
                suffix = match.group(2)
                if suffix:
                    if re.compile(self.regexes['match']['suffix-bce']).match(suffix) is not None:
                        years = (100 * -century, 100 * -century + 99)
                    else:
                        years = (100 * (century - 1), 100 * (century - 1) + 99)
                else:
                    years = (100 * (century - 1), 100 * (century - 1) + 99)
            elif m[1] != '':
                # explicit year-range

                # FIXME: spaghetti code, but it works!
                range_of_stuff = []
                i = 0
                first_none = None
                for y in re.sub(self.regexes['substitution']['year-year-splitter'], r'\1>|<\2', m[1]).split('>|<'):
                    # get rid of whitespace
                    y = y.strip()
                    match = re.compile(self.regexes['capture']['year']).match(y)

                    # if there is a suffix, one of these will not be None
                    suffix = match.group(2) or match.group(4)
                    if suffix:
                        if i == 0:
                            first_none = False

                        if re.compile(self.regexes['match']['suffix-bce']).match(suffix) is not None:
                            range_of_stuff.append(-1 * int(match.group(1) or match.group(3)))
                        else:
                            range_of_stuff.append(int(match.group(1) or match.group(3)))
                    else:
                        if i == 0:
                            first_none = True
                        range_of_stuff.append(int(match.group(1) or match.group(3)))

                    i += 1
                if first_none:
                    if range_of_stuff[1] <= 0:
                        range_of_stuff[0] = -1 * range_of_stuff[0]

                years = (range_of_stuff[0], range_of_stuff[1])
            elif m[2] != '':
                # extract single year
                prep = re.sub(self.regexes['substitution']['dd-mon-year-time'], r'\1', m[2]).strip()
                years = int(prep)
            elif m[3] != '':
                # year with unknown ones
                y = m[3].strip()
                match = re.compile(r'[1-9]\d{3}').match(y)
                if match is None:
                    years = int(self._resolve_unknown_ones(y))

                else:
                    years = int(match.group(0))

            elif m[4] != '':
                # plain old year
                match = re.compile(self.regexes['capture']['year']).match(m[4])
                suffix = match.group(2) or match.group(4)
                if suffix:
                    if re.compile(self.regexes['match']['suffix-bce']).match(suffix) is not None:
                        years = -1 * int(match.group(1) or match.group(3))
                    else:
                        years = int(match.group(1) or match.group(3))
                else:
                    years = int(match.group(1) or match.group(3))
            else:
                raise Error

        except ValueError as e:
            #logger.error('An error occurred while trying to match "{}": {}'.format(m, e))
            pass

        #logger.debug('Mapping match to years: {} -> {}'.format(m, years))
        return years

    def _enumerate_decades(self, preprocessed_data, disjoint):
        """Return a set of decades. If disjoint is false, returns a set of decades that spans the entire range.

        preprocessed_data - a heterogeneous set of ints and tuples of ints
        """

        if disjoint:
            decades = set()
            for decade in preprocessed_data:
                if isinstance(decade, int):
                    decades.add(decade // 10 * 10)
                elif isinstance(decade, tuple):
                    decades = decades | set(range(decade[0] // 10 * 10, decade[1] + 1, 10))

            return decades

    def _enumerate_years(self, preprocessed_data, disjoint):
        """Return a set of years. If disjoint is false, returns a set of years that spans the entire range.

        preprocessed_data - a heterogeneous set of ints and tuples of ints
        """
        pass

    def _extract_year_data(self, date_string):
        """Extracts the year(s) and/or year range(s) embedded in the date_string, and return a set of ints (year) and/or tuples of ints (year range, start/end).

        date_string - the string containing the dirty date
        """

        try:
            # first see if dateutil can parse the date string
            # simplest case, a single year
            return {parse(date_string).year}

        except ValueError:
            try:
                # strip alphabetical chars and spaces from the left side and try again
                alpha = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
                return {parse(date_string.lstrip(alpha + ' ')).year}

            except ValueError:
                # find as many substrings that look like dates as possible
                matches = re.findall(re.compile(self.regexes['match']['date']), date_string)
                #logger.debug('{} date string matches found in "{}"'.format(len(matches), dateString))
                if len(matches) > 0:
                    return {self._date_match_to_int_or_tuple(m) for m in matches}
                else:
                    return set()

    def _resolve_unknown_ones(self, i):
        """i is a string that represents a year with a possibly missing ones value, like "199-?" or "199?". Round down to the nearest decade.
        """

        m = re.compile('(^\\d{4}$)').match(i)
        if m is not None:
            return int(m.group(1))
        else:
            m = re.compile('(^\\d{1,3})[-*?]$').match(i)
            return i if m is None else int(m.group(1) + '0')
