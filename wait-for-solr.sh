#!/bin/bash

# Adapted from the example at: https://docs.docker.com/compose/startup-order/

set -e

if [[ -z ${SOLR_HOST} ]]
then
    # This string must match the id of the Solr service in docker-compose.yml
    export SOLR_HOST="solr"
fi

if [[ -z ${SOLR_PING_TIMEOUT} ]]
then
    export SOLR_PING_TIMEOUT=15
fi

SECONDS=0

until curl -s "http://${SOLR_HOST}:${SOLR_PORT}/solr/${SOLR_CORE_NAME}/admin/ping"; do

    if [[ ${SOLR_PING_TIMEOUT} -gt 0 && ${SECONDS} -ge ${SOLR_PING_TIMEOUT} ]]
    then
        >&2 echo "Solr is unavailable after ${SOLR_PING_TIMEOUT} seconds - exiting"
        exit 1
    fi

    >&2 echo "Solr is unavailable - sleeping"
    sleep 1
done

>&2 echo "Solr is up - executing command"
exec "$@"
