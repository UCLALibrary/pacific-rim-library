FROM python:3.6

SHELL ["/bin/bash", "-c"]

WORKDIR /root

COPY setup.py .
RUN python setup.py install

COPY logging.yml .
RUN mkdir /var/log/prl

ARG LEVELDB_HARVESTER_SETTINGS_DIRECTORY
ARG LEVELDB_RECORD_SETS_DIRECTORY
ARG THUMBNAILS_DIRECTORY

COPY pacific_rim_library pacific_rim_library
RUN mkdir -p ${LEVELDB_HARVESTER_SETTINGS_DIRECTORY} ${LEVELDB_RECORD_SETS_DIRECTORY} ${THUMBNAILS_DIRECTORY} \
    && python -m pacific_rim_library.configure

COPY wait-for-solr.sh .
RUN chmod +x wait-for-solr.sh
