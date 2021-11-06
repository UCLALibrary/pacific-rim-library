#!/bin/bash

# Adapted from the example at: https://docs.docker.com/compose/startup-order/

set -e

if [[ -z ${SOLR_HOST} ]]
then
    # This string must match the id of the Solr service in docker-compose.yml
    export SOLR_HOST="solr"
fi

until curl -s "http://${SOLR_HOST}:${SOLR_PORT}/solr/prl/admin/ping"; do
    >&2 echo "Solr is unavailable - sleeping"
    sleep 1
done

>&2 echo "Solr is up - executing command"
exec "$@"
