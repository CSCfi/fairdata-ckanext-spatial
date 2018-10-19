import re
import urllib
import urlparse

import logging

from ckan import model

from ckan.plugins.core import SingletonPlugin, implements

from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.model import HarvestObjectExtra as HOExtra

from ckanext.spatial.lib.csw_client import CswService
from ckanext.spatial.harvesters.base import SpatialHarvester, text_traceback

from ckanext.etsin.data_catalog_service import ensure_data_catalog_ok


class CSWHarvester(SpatialHarvester, SingletonPlugin):
    '''
    A Harvester for CSW servers
    '''
    implements(IHarvester)

    csw = None

    def info(self):
        return {
            'name': 'csw',
            'title': 'CSW Server',
            'description': 'A server that implements OGC\'s Catalog Service for the Web (CSW) standard'
        }

    def get_original_url(self, harvest_object_id):
        obj = model.Session.query(HarvestObject). \
            filter(HarvestObject.id == harvest_object_id). \
            first()

        parts = urlparse.urlparse(obj.source.url)

        params = {
            'SERVICE': 'CSW',
            'VERSION': '2.0.2',
            'REQUEST': 'GetRecordById',
            'OUTPUTSCHEMA': 'http://www.isotc211.org/2005/gmd',
            'OUTPUTFORMAT': 'application/xml',
            'ID': obj.guid
        }

        url = urlparse.urlunparse((
            parts.scheme,
            parts.netloc,
            parts.path,
            None,
            urllib.urlencode(params),
            None
        ))

        return url

    def output_schema(self):
        return 'gmd'

    def gather_stage(self, harvest_job):
        log = logging.getLogger(__name__ + '.CSW.gather')
        log.info('CSW Harvester gather_stage for job: %r', harvest_job)

        self._set_source_config(harvest_job.source.config)
        cql = self.source_config.get('cql')
        identifier_schema = self.source_config.get('identifier_schema', self.output_schema())
        url = harvest_job.source.url

        log.info('Harvest source: %s', url)

        # Data catalog related operations
        if not ensure_data_catalog_ok(self.source_config.get('harvest_source_name', '')):
            return []

        try:
            self._setup_csw_client(url)
        except Exception, e:
            self._save_gather_error('Error contacting the CSW server: %s' % e, harvest_job)
            return None

        guids_in_source = set()
        try:
            for identifier in self.csw.getidentifiers(page=10, outputschema=identifier_schema, cql=cql):
                try:
                    if identifier is None:
                        log.error('CSW did not return identifier, skipping...')
                        continue
                    guids_in_source.add(identifier)
                except Exception, e:
                    self._save_gather_error('Error for the identifier %s [%r]' % (identifier, e), harvest_job)
                    continue
        except Exception, e:
            log.error('Exception: %s' % text_traceback())
            self._save_gather_error('Error gathering the identifiers from the CSW server [%s]' % str(e), harvest_job)
            return None

        try:
            object_ids = []
            if len(guids_in_source):
                log.debug('Record identifiers: %s', guids_in_source)

                harvest_objs_in_db = model.Session.query(HarvestObject.guid, HarvestObject.package_id). \
                    filter(HarvestObject.current == True). \
                    filter(HarvestObject.harvest_source_id == harvest_job.source.id)

                db_harvest_obj_guid_to_package_id_map = {}
                for guid, package_id in harvest_objs_in_db:
                    db_harvest_obj_guid_to_package_id_map[guid] = package_id

                current_guids_in_db = set(db_harvest_obj_guid_to_package_id_map.keys())

                new_guids = guids_in_source - current_guids_in_db
                deleted_guids = current_guids_in_db - guids_in_source
                existing_guids = current_guids_in_db & guids_in_source

                for guid in new_guids:
                    obj = HarvestObject(guid=guid, job=harvest_job,
                                        extras=[HOExtra(key='status', value='new')])
                    obj.save()
                    object_ids.append(obj.id)
                for guid in existing_guids:
                    obj = HarvestObject(guid=guid, job=harvest_job,
                                        package_id=db_harvest_obj_guid_to_package_id_map[guid],
                                        extras=[HOExtra(key='status', value='change')])
                    obj.save()
                    object_ids.append(obj.id)
                for guid in deleted_guids:
                    obj = HarvestObject(guid=guid, job=harvest_job,
                                        package_id=db_harvest_obj_guid_to_package_id_map[guid],
                                        extras=[HOExtra(key='status', value='delete')])
                    model.Session.query(HarvestObject). \
                        filter_by(guid=guid). \
                        update({'current': False}, False)
                    obj.save()
                    object_ids.append(obj.id)

                log.debug('Harvest object IDs: {i}'.format(i=object_ids))
                return object_ids

            else:
                self._save_gather_error('No records received from URL: {u}'.format(
                    u=harvest_job.source.url), harvest_job)
                return None
        except Exception as e:
            self._save_gather_error('Gather: {e}'.format(e=e), harvest_job)
            raise

    def fetch_stage(self, harvest_object):

        # Check harvest object status
        status = self._get_object_extra(harvest_object, 'status')

        if status == 'delete':
            # No need to fetch anything, just pass to the import stage
            return True

        log = logging.getLogger(__name__ + '.CSW.fetch')
        log.debug('CswHarvester fetch_stage for object: %s', harvest_object.id)

        url = harvest_object.source.url
        try:
            self._setup_csw_client(url)
        except Exception, e:
            log.error('Error contacting the CSW server')
            self._save_object_error('Error contacting the CSW server: %s' % e,
                                    harvest_object)
            return False

        identifier = harvest_object.guid
        esn = self.source_config.get('esn', 'full')
        try:
            record = self.csw.getrecordbyid([identifier], outputschema=self.output_schema(), esn=esn)
        except Exception, e:
            log.error('Error getting the CSW record with GUID %s' % identifier)
            self._save_object_error('Error getting the CSW record with GUID %s' % identifier, harvest_object)
            return False

        if record is None:
            self._save_object_error('Empty record for GUID %s' % identifier,
                                    harvest_object)
            return False

        try:
            # Save the fetch contents in the HarvestObject
            # Contents come from csw_client already declared and encoded as utf-8
            # Remove original XML declaration
            content = re.sub('<\?xml(.*)\?>', '', record['xml'])

            harvest_object.content = content.strip()
            harvest_object.save()
        except Exception, e:
            self._save_object_error('Error saving the harvest object for GUID %s [%r]' % \
                                    (identifier, e), harvest_object)
            return False

        log.debug('XML content saved (len %s)', len(record['xml']))
        return True

    def _setup_csw_client(self, url):
        self.csw = CswService(url)
