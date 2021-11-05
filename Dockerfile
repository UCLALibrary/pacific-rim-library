FROM python:3.6

SHELL ["/bin/bash", "-c"]

WORKDIR /root

# Solr must be listening on port 8983 before starting the PRL indexer.
RUN wget -q https://raw.githubusercontent.com/vishnubob/wait-for-it/master/wait-for-it.sh
RUN chmod +x wait-for-it.sh

COPY setup.py .
RUN python setup.py install

COPY logging.yml .
RUN mkdir /var/log/prl

COPY pacific_rim_library pacific_rim_library
RUN python -m pacific_rim_library.configure
