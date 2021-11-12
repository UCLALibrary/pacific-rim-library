#!/usr/bin/python3

"""Configuration script for PRL."""

import argparse
from json import dump, load
import os
from shutil import copyfile
from typing import Dict


DEFAULTS = {
    'config': {
        'dir': '~/.pacific_rim_library',
        'meta': 'pacific_rim_library.json',
        'files': {
            'logging': 'logging.yml'
        }
    }
}


def get_config():
    '''Returns the saved configuration.'''
    with open(os.path.expanduser(os.path.join(DEFAULTS['config']['dir'], DEFAULTS['config']['meta'])), 'r') as config_dir_file:
        return load(config_dir_file)


class Configure(object):
    """Configuration script for PRL."""

    def __init__(self, args: Dict[str, str]):
        print('Saving configuration to {}'.format(args['config_dir']))

        os.makedirs(os.path.expanduser(args['config_dir']), exist_ok=True)

        # Copy the logging config file to the config directory.
        copyfile(
            args['logging_config'],
            os.path.expanduser(os.path.join(args['config_dir'], args['logging_config'])))
 
        # Write the config to the default <config>/<meta>, so our app can find it.
        config = DEFAULTS['config'].copy()
        config['dir'] = args['config_dir']
        config['files']['logging'] = args['logging_config']

        os.makedirs(os.path.expanduser(DEFAULTS['config']['dir']), exist_ok=True)
        with open(os.path.expanduser(os.path.join(DEFAULTS['config']['dir'], DEFAULTS['config']['meta'])), 'w') as config_dir_file:
            dump(config, config_dir_file)


if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description='Configuration script for the Pacific Rim Library back-end.')
    parser.add_argument(
        '-c', '--config-dir',
        metavar='PATH',
        action='store',
        default=DEFAULTS['config']['dir'],
        help='directory for configuration files related to the indexer')
    parser.add_argument(
        '-l', '--logging-config',
        metavar='PATH',
        action='store',
        default=DEFAULTS['config']['files']['logging'],
        help='logging configuration file')

    configure_args = vars(parser.parse_args())
    Configure(configure_args)
