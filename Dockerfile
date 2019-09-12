FROM python:3.6

ARG AWS_ACCESS_KEY_ID
ARG AWS_SECRET_ACCESS_KEY
ARG AWS_REGION

SHELL ["/bin/bash", "-c"]

WORKDIR /root

# Solr must be listening on port 8983 before starting the PRL indexer.
RUN wget -q https://raw.githubusercontent.com/vishnubob/wait-for-it/master/wait-for-it.sh
RUN chmod +x wait-for-it.sh

RUN pip install --trusted-host pypi.python.org awscli --upgrade

RUN aws configure --profile dev-prl-images set aws_access_key_id $AWS_ACCESS_KEY_ID
RUN aws configure --profile dev-prl-images set aws_secret_access_key $AWS_SECRET_ACCESS_KEY
RUN aws configure --profile dev-prl-images set region $AWS_REGION

COPY pacific_rim_library pacific_rim_library
COPY setup.py .
RUN python setup.py install

COPY config.toml logging.yml .
RUN mkdir /var/log/prl

RUN python -m pacific_rim_library.configure