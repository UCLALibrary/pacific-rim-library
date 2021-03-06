# pacific-rim-library

This repository contains the `pacific_rim_library` Python package. The main executable module is `indexer`.

At a high level, `indexer` watches for two classes of events (create/update and delete) on regular files under a user-specified directory. It expects these files to only ever be Dublin Core XML. 

On create/update events, it transforms files into Solr documents and adds them to an index, and copies any relevant images (whose HTTP URLs are embedded in the XML) to a S3 bucket.

On delete events, it removes any traces of the record represented by the deleted file from Solr and S3.

## Installation

### Docker

1. Install Docker.
1. Download and extract this repository.
1. Fill in the blanks in `config.toml`.
1. Fill in the blanks in `.env` with the AWS credentials for your S3 bucket.
1. Edit `logging.yml` to configure logging as desired.
1. Create a local `prl-solr` Docker image:

    1. Create a local `solr4` Docker image by following the  instructions at https://github.com/docker-solr/docker-solr4 (you must be authenticated to hub.docker.com)
    1. Clone https://github.com/UCLALibrary/prrla-solr-conf and then run something like:

        ```bash
        $ docker image build . --tag prl-solr:latest
        ```

1. Build and run the containers (this will result in a "file not found error"):

    ```bash
    $ docker-compose -p prl build
    $ docker-compose -p prl up
    ```

1. [Ingest content into PRL via jOAI](https://docs.library.ucla.edu/display/dlp/PRL+content+ingest) and [initialize the Solr index with institution records](https://docs.library.ucla.edu/pages/viewpage.action?pageId=161622923).
1. Restart all the containers.
1. Access jOAI by first visiting http://localhost:8080 (it crashes otherwise), then go to http://localhost:8080/oai.

### Native

1. Install Python 3.4 or greater and the AWS CLI.
2. Download and extract this repository.
3. Create target directories for harvested files:

    ```bash
    mkdir ~/prl-records ~/prl-thumbnails
    ```

4. Add a AWS CLI profile for accessing the thumbnails S3 bucket (be sure to specify the bucket region):

    ```bash
    aws configure --profile prl-thumbnails
    ```

5. (OPTIONAL) If setting up a development environment, install a Python 3 virtual environment manager and create an environment:

    ```bash
    # Ubuntu
    sudo apt-get install python3-venv
    python3 -m venv venv-prl
    ```

    ```bash
    # OSX
    pip3 install virtualenv
    python3 -m virtualenv venv-prl
    ```

    Fire it up:
    ```bash
    source venv-prl/bin/activate
    ```

6. Install the latest `setuptools`:

    ```bash
    pip3 install --upgrade setuptools
    ```

7. Install Python dependencies:

    ```bash
    python3 setup.py install
    ```
    If `plyvel` fails to install, try installing with `pip3` and then re-do this step.

    On OSX: if `lxml` fails to install, you may need to install it with `STATIC_DEPS` set to `true` per https://lxml.de/installation.html.

8. Fill in the blanks in `config.toml`.

9. Edit `logging.yml` to configure logging as desired.

10. Install the configuration files:

    ```bash
    python3 -m pacific_rim_library.configure
    ```

## Usage

The module is meant to be run as a background process. For usage instructions:

```bash
python3 -m pacific_rim_library.indexer --help
```

## Tests

To run automated tests:

```bash
python3 setup.py test
```
