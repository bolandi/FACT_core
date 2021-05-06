import json
import logging
import pickle
import random
from hashlib import md5
from typing import Dict, List, Set

import gridfs
from common_helper_files import get_safe_name
from common_helper_mongo.aggregate import get_all_value_combinations_of_fields, get_list_of_all_values

from helperFunctions.data_conversion import convert_time_to_str, get_dict_size
from objects.file import FileObject
from objects.firmware import Firmware
from storage.mongo_interface import MongoInterface


class MongoInterfaceCommon(MongoInterface):  # pylint: disable=too-many-instance-attributes

    def _setup_database_mapping(self):
        main_database = self.config['data_storage']['main_database']
        self.main = self.client[main_database]
        self.firmwares = self.main.firmwares
        self.file_objects = self.main.file_objects
        self.search_query_cache = self.main.search_query_cache
        self.locks = self.main.locks
        # sanitize stuff
        self.report_threshold = int(self.config['data_storage']['report_threshold'])
        sanitize_db = self.config['data_storage'].get('sanitize_database', 'faf_sanitize')
        self.sanitize_storage = self.client[sanitize_db]
        self.sanitize_fs = gridfs.GridFS(self.sanitize_storage)

    def existence_quick_check(self, uid):
        if self.is_firmware(uid):
            return True
        if self.is_file_object(uid):
            return True
        return False

    def is_firmware(self, uid):
        return self.firmwares.count_documents({'_id': uid}) > 0

    def is_file_object(self, uid):
        return self.file_objects.count_documents({'_id': uid}) > 0

    def get_object(self, uid, analysis_filter=None):
        '''
        input uid
        output:
            - firmware_object if uid found in firmware database
            - else: file_object if uid found in file_database
            - else: None
        '''
        fo = self.get_file_object(uid, analysis_filter=analysis_filter)
        if fo is None:
            fo = self.get_firmware(uid, analysis_filter=analysis_filter)
        return fo

    def get_complete_object_including_all_summaries(self, uid):
        '''
        input uid
        output:
            like get_object, but includes all summaries and list of all included files set
        '''
        fo = self.get_object(uid)
        if fo is None:
            raise Exception('UID not found: {}'.format(uid))
        fo.list_of_all_included_files = self.get_list_of_all_included_files(fo)
        for analysis in fo.processed_analysis:
            fo.processed_analysis[analysis]['summary'] = self.get_summary(fo, analysis)
        return fo

    def get_firmware(self, uid, analysis_filter=None):
        firmware_entry = self.firmwares.find_one(uid)
        if firmware_entry:
            return self._convert_to_firmware(firmware_entry, analysis_filter=analysis_filter)
        logging.debug('No firmware with UID {} found.'.format(uid))
        return None

    def get_file_object(self, uid, analysis_filter=None):
        file_entry = self.file_objects.find_one(uid)
        if file_entry:
            return self._convert_to_file_object(file_entry, analysis_filter=analysis_filter)
        logging.debug('No FileObject with UID {} found.'.format(uid))
        return None

    def get_objects_by_uid_list(self, uid_list, analysis_filter=None):
        if not uid_list:
            return []
        query = self._build_search_query_for_uid_list(uid_list)
        results = [self._convert_to_firmware(i, analysis_filter=analysis_filter) for i in self.firmwares.find(query) if i is not None]
        results.extend([self._convert_to_file_object(i, analysis_filter=analysis_filter) for i in self.file_objects.find(query) if i is not None])
        return results

    @staticmethod
    def _build_search_query_for_uid_list(uid_list):
        query = {'_id': {'$in': list(uid_list)}}
        return query

    def _convert_to_firmware(self, entry: dict, analysis_filter: List[str] = None) -> Firmware:
        firmware = Firmware()
        firmware.uid = entry['_id']
        firmware.size = entry['size']
        firmware.file_name = entry['file_name']
        firmware.set_device_name(entry['device_name'])
        firmware.set_device_class(entry['device_class'])
        firmware.set_release_date(convert_time_to_str(entry['release_date']))
        firmware.set_vendor(entry['vendor'])
        firmware.set_firmware_version(entry['version'])
        firmware.processed_analysis = self.retrieve_analysis(entry['processed_analysis'], analysis_filter=analysis_filter)
        firmware.files_included = set(entry['files_included'])
        firmware.virtual_file_path = entry['virtual_file_path']
        firmware.tags = entry['tags'] if 'tags' in entry else dict()
        firmware.analysis_tags = self._collect_analysis_tags_from_children(firmware.uid)

        try:  # for backwards compatibility
            firmware.set_part_name(entry['device_part'])
        except KeyError:
            firmware.set_part_name('complete')

        if 'comments' in entry:  # for backwards compatibility
            firmware.comments = entry['comments']
        return firmware

    def _convert_to_file_object(self, entry: dict, analysis_filter: List[str] = None) -> FileObject:
        file_object = FileObject()
        file_object.uid = entry['_id']
        file_object.size = entry['size']
        file_object.file_name = entry['file_name']
        file_object.virtual_file_path = entry['virtual_file_path']
        file_object.parents = entry['parents']
        file_object.processed_analysis = self.retrieve_analysis(entry['processed_analysis'], analysis_filter=analysis_filter)
        file_object.files_included = set(entry['files_included'])
        file_object.parent_firmware_uids = set(entry['parent_firmware_uids'])
        file_object.analysis_tags = {}
        self._collect_analysis_tags(file_object, file_object.analysis_tags)

        for attribute in ['comments']:  # for backwards compatibility
            if attribute in entry:
                setattr(file_object, attribute, entry[attribute])
        return file_object

    def sanitize_analysis(self, analysis_dict, uid):
        sanitized_dict = {}
        for key in analysis_dict.keys():
            if get_dict_size(analysis_dict[key]) > self.report_threshold:
                logging.debug('Extracting analysis {} to file (Size: {})'.format(key, get_dict_size(analysis_dict[key])))
                sanitized_dict[key] = self._extract_binaries(analysis_dict, key, uid)
                sanitized_dict[key]['file_system_flag'] = True
            else:
                sanitized_dict[key] = analysis_dict[key]
                sanitized_dict[key]['file_system_flag'] = False
        return sanitized_dict

    def retrieve_analysis(self, sanitized_dict, analysis_filter=None):
        '''
        retrieves analysis including sanitized entries
        :param sanitized_dict: processed analysis dictionary including references to sanitized entries
        :type dict:
        :param analysis_filter: list of analysis plugins to be restored
        :type list:
        :default None:
        :return: dict
        '''
        if analysis_filter is None:
            analysis_filter = sanitized_dict.keys()
        for key in analysis_filter:
            try:
                if sanitized_dict[key]['file_system_flag']:
                    logging.debug('Retrieving stored file {}'.format(key))
                    sanitized_dict[key].pop('file_system_flag')
                    sanitized_dict[key] = self._retrieve_binaries(sanitized_dict, key)
                else:
                    sanitized_dict[key].pop('file_system_flag')
            except (KeyError, IndexError, AttributeError, TypeError, pickle.PickleError) as error:
                logging.debug('Could not retrieve information: {} {}'.format(type(error), error))
        return sanitized_dict

    def _extract_binaries(self, analysis_dict, key, uid):
        tmp_dict = {}
        for analysis_key in analysis_dict[key].keys():
            if analysis_key != 'summary':
                file_name = '{}_{}_{}'.format(get_safe_name(key), get_safe_name(analysis_key), uid)
                self._store_in_sanitize_db(pickle.dumps(analysis_dict[key][analysis_key]), file_name)
                tmp_dict[analysis_key] = file_name
            else:
                tmp_dict[analysis_key] = analysis_dict[key][analysis_key]
        return tmp_dict

    def _store_in_sanitize_db(self, content: bytes, file_name: str):
        if self.sanitize_fs.exists({'filename': file_name}):
            md5_hash = md5(content).hexdigest()
            if self.sanitize_fs.exists({'md5': md5_hash}):
                return  # there is already an up to date entry -> do nothing
            for old_entry in self.sanitize_fs.find({'filename': file_name}):  # delete old entries first
                logging.debug('deleting old sanitize db entry of {} with id {}'.format(file_name, old_entry._id))  # pylint: disable=protected-access
                self.sanitize_fs.delete(old_entry._id)  # pylint: disable=protected-access
        self.sanitize_fs.put(content, filename=file_name)

    def _retrieve_binaries(self, sanitized_dict, key):
        tmp_dict = {}
        for analysis_key in sanitized_dict[key].keys():
            if analysis_key == 'summary' and not isinstance(sanitized_dict[key][analysis_key], str):
                tmp_dict[analysis_key] = sanitized_dict[key][analysis_key]
            else:
                logging.debug('Retrieving {}'.format(analysis_key))
                tmp = self.sanitize_fs.get_last_version(sanitized_dict[key][analysis_key])
                if tmp is not None:
                    report = pickle.loads(tmp.read())
                else:
                    logging.error('sanitized file not found: {}'.format(sanitized_dict[key][analysis_key]))
                    report = {}
                tmp_dict[analysis_key] = report
        return tmp_dict

    def get_specific_fields_of_db_entry(self, uid, field_dict):
        return self.file_objects.find_one(uid, field_dict) or self.firmwares.find_one(uid, field_dict)

    # --- summary recreation

    def get_list_of_all_included_files(self, fo):
        if isinstance(fo, Firmware):
            fo.list_of_all_included_files = get_list_of_all_values(
                self.file_objects, '$_id', match={'virtual_file_path.{}'.format(fo.uid): {'$exists': 'true'}})
        if fo.list_of_all_included_files is None:
            fo.list_of_all_included_files = list(self.get_set_of_all_included_files(fo))
        fo.list_of_all_included_files.sort()
        return fo.list_of_all_included_files

    def get_set_of_all_included_files(self, fo):
        '''
        return a set of all included files uids
        the set includes fo uid as well
        '''
        if fo is not None:
            files = {fo.uid}
            included_files = self.get_objects_by_uid_list(fo.files_included, analysis_filter=[])
            for item in included_files:
                files.update(self.get_set_of_all_included_files(item))
            return files
        return set()

    def get_uids_of_all_included_files(self, uid: str) -> Set[str]:
        return {
            match['_id']
            for match in self.file_objects.find({'parent_firmware_uids': uid}, {'_id': 1})
        }

    def get_summary(self, fo, selected_analysis):
        if selected_analysis not in fo.processed_analysis:
            logging.warning('Analysis {} not available on {}'.format(selected_analysis, fo.uid))
            return None
        if 'summary' not in fo.processed_analysis[selected_analysis]:
            return None
        if not isinstance(fo, Firmware):
            return self._collect_summary(fo.list_of_all_included_files, selected_analysis)
        summary = get_all_value_combinations_of_fields(
            self.file_objects, '$processed_analysis.{}.summary'.format(selected_analysis), '$_id',
            unwind=True, match={'virtual_file_path.{}'.format(fo.uid): {'$exists': 'true'}})
        fo_summary = self._get_summary_of_one(fo, selected_analysis)
        self._update_summary(summary, fo_summary)
        return summary

    @staticmethod
    def _get_summary_of_one(file_object, selected_analysis):
        summary = {}
        try:
            if 'summary' in file_object.processed_analysis[selected_analysis].keys():
                for item in file_object.processed_analysis[selected_analysis]['summary']:
                    summary[item] = [file_object.uid]
        except (AttributeError, KeyError) as err:
            logging.warning('Could not get summary: {} {}'.format(type(err), err))
        return summary

    def _collect_summary(self, uid_list, selected_analysis):
        summary = {}
        file_objects = self.get_objects_by_uid_list(uid_list, analysis_filter=[selected_analysis])
        for fo in file_objects:
            summary = self._update_summary(summary, self._get_summary_of_one(fo, selected_analysis))
        return summary

    @staticmethod
    def _update_summary(original_dict, update_dict):
        for item in update_dict:
            if item in original_dict:
                original_dict[item].extend(update_dict[item])
            else:
                original_dict[item] = update_dict[item]
        return original_dict

    def get_firmware_number(self, query=None):
        if query is not None and isinstance(query, str):
            query = json.loads(query)
        return self.firmwares.count_documents(query or {})

    def get_file_object_number(self, query=None, zero_on_empty_query=True):
        if isinstance(query, str):
            query = json.loads(query)
        if zero_on_empty_query and query == {}:
            return 0
        return self.file_objects.count_documents(query or {})

    def set_unpacking_lock(self, uid):
        self.locks.insert_one({'uid': uid})

    def check_unpacking_lock(self, uid):
        return self.locks.count_documents({'uid': uid}) > 0

    def release_unpacking_lock(self, uid):
        self.locks.delete_one({'uid': uid})

    def drop_unpacking_locks(self):
        self.main.drop_collection('locks')

    def _collect_analysis_tags_from_children(self, uid):
        children = self._fetch_children_with_tags(uid)
        unique_tags = {}
        for child in children:
            self._collect_analysis_tags(child, unique_tags)
        return unique_tags

    def _collect_analysis_tags(self, file_object, analysis_tags):
        for name, analysis in [(n, a) for n, a in file_object.processed_analysis.items() if 'tags' in a]:
            if 'file_system_flag' in analysis and analysis['file_system_flag']:
                analysis = self.retrieve_analysis(file_object.processed_analysis, analysis_filter=[name, ])[name]

            for tag_type, tag in analysis['tags'].items():
                if tag_type != 'root_uid' and tag['propagate']:
                    append_unique_tag(analysis_tags, tag, name, tag_type)

    def _fetch_children_with_tags(self, uid):
        plugins_with_tag_propagation = [  # FIXME This should be inferred in a sensible way. This is not possible yet.
            'crypto_material', 'cve_lookup', 'known_vulnerabilities', 'qemu_exec', 'software_components',
            'users_and_passwords'
        ]
        uids = set()
        for plugin in plugins_with_tag_propagation:
            uids.update(
                set(
                    get_list_of_all_values(
                        self.file_objects,
                        '$_id',
                        match={
                            'virtual_file_path.{}'.format(uid): {'$exists': 'true'},
                            f'processed_analysis.{plugin}.tags': {'$exists': 'true'}
                        }
                    )
                )
            )
        return self.get_objects_by_uid_list(uids, analysis_filter=plugins_with_tag_propagation)


def append_unique_tag(unique_tags: Dict[str, dict], tag: dict, plugin_name: str, tag_type: str) -> None:
    if plugin_name in unique_tags:
        if tag_type in unique_tags[plugin_name] and tag not in unique_tags[plugin_name].values():
            unique_tags[plugin_name][f'{tag_type}-alt-{random.randint(0, 1000)}'] = tag
        else:
            unique_tags[plugin_name][tag_type] = tag
    else:
        unique_tags[plugin_name] = {tag_type: tag}
