FROM python:3.6

SHELL ["/bin/bash", "-c"]

WORKDIR /root

COPY setup.py .
RUN python setup.py install

COPY logging.yml .
RUN mkdir /var/log/prl

COPY pacific_rim_library pacific_rim_library
RUN python -m pacific_rim_library.configure

COPY wait-for-solr.sh .
RUN chmod +x wait-for-solr.sh
