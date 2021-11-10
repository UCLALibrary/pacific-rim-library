#!/usr/bin/python3
# -*- coding: utf-8 -*-

import io
import os
import sys
from shutil import rmtree

from setuptools import find_packages, setup, Command

NAME = 'pacific_rim_library'
DESCRIPTION = 'Indexer for the Pacific Rim Library back-end.'
URL = 'https://github.com/UCLALibrary/pacific-rim-library'
EMAIL = 'mmatney@library.ucla.edu'
AUTHOR = 'Mark Allen Matney, Jr.'
REQUIRES_PYTHON = '>=3.4.0'
VERSION = '0.1.0'
REQUIRED = [
    'beautifulsoup4~=4.6.0',
    'boto3~=1.4.7',
    'Django~=2.2.24',
    'javaobj-py3~=0.4.0',
    'lxml~=4.6.3',
    'plyvel~=1.0.5',
    'pysolr~=3.8.0',
    'requests~=2.22.0',
    'sickle~=0.6.4',
    'toml~=0.10.0',
    'watchdog~=0.9.0',
    'PyYAML~=5.4'
]
REQUIRED_SETUP = [
    'pytest-runner~=4.2',
    'pytest-pylint~=0.12.3'
]
REQUIRED_TESTS = [
    'pytest~=3.10.0',
    'pylint~=2.1.1'
]

# The rest you shouldn't have to touch too much :)
# ------------------------------------------------
# Except, perhaps the License and Trove Classifiers!
# If you do change the License, remember to change the Trove Classifier for that!

here = os.path.abspath(os.path.dirname(__file__))

# Import the README and use it as the long-description.
# Note: this will only work if 'README.md' is present in your MANIFEST.in file!
try:
    with io.open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
        long_description = '\n' + f.read()
except FileNotFoundError:
    long_description = DESCRIPTION

about = {}
if not VERSION:
    with open(os.path.join(here, NAME, '__version__.py')) as f:
        exec(f.read(), about)
else:
    about['__version__'] = VERSION

setup(
    name=NAME,
    version=about['__version__'],
    description=DESCRIPTION,
    long_description=long_description,
    author=AUTHOR,
    author_email=EMAIL,
    python_requires=REQUIRES_PYTHON,
    url=URL,
    packages=find_packages(),
    install_requires=REQUIRED,
    setup_requires=REQUIRED_SETUP,
    tests_require=REQUIRED_TESTS,
    include_package_data=True
)
