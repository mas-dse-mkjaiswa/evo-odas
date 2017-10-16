import logging
import pprint
import os
import six
import fnmatch
from airflow.operators import BashOperator
from airflow.operators import BaseOperator
from airflow.operators import PythonOperator
from airflow.plugins_manager import AirflowPlugin
from airflow.utils.decorators import apply_defaults
from airflow.models import XCOM_RETURN_KEY

from zipfile import ZipFile
import config.xcom_keys as xk
#from config import xcom_keys as xk
from sentinel1.utils.ssat1_metadata import create_procuct_zip

log = logging.getLogger(__name__)


class ZipInspector(BaseOperator):
    @apply_defaults
    def __init__(self, extension_to_search,  get_inputs_from=None, *args, **kwargs):
        self.substring = extension_to_search
        self.get_inputs_from = get_inputs_from
        log.info('--------------------GDAL_PLUGIN Zip inspector------------')
        super(ZipInspector, self).__init__(*args, **kwargs)

    def execute(self, context):
        zip_files=list()
        if self.get_inputs_from != None:
            zip_files = context['task_instance'].xcom_pull(task_ids=self.get_inputs_from, key=XCOM_RETURN_KEY)
        else:
            #legacy 
            log.info('**** Inside execute ****')
            log.info("ZipInspector Operator params list")
            log.info('Substring to search: %s', self.substring)

            downloaded = context['task_instance'].xcom_pull(task_ids='dhus_download_task', key='downloaded_products')
            zip_files = downloaded.keys()
        
        # stop processing if there are no products
        if zip_files is None or len(zip_files)==0:
            log.info("Nothing to process.")
            return

        return_dict=dict()
        log.info("Processing {} ZIP files:\n{} ".format(len(zip_files),pprint.pformat(zip_files)))
        for zip_file in zip_files:
            log.info("Processing {}..".format(zip_file))
            counter = 0;
            vsi_paths=list()
            zip = ZipFile(zip_file)
            for file in zip.namelist():
                filename = zip.getinfo(file).filename
                if self.substring in filename:
                    counter = counter + 1
                    raster_vsizip = "/vsizip/" + zip_file + "/" + filename
                    vsi_paths.append(raster_vsizip)
                    log.info(str(counter) + ") '" + raster_vsizip + "'")
                    context['task_instance'].xcom_push(key=xk.IMAGE_ZIP_ABS_PATH_PREFIX_XCOM_KEY + str(counter), value=raster_vsizip)
            return_dict[zip_file]=vsi_paths

        return return_dict

class RSYNCOperator(BaseOperator):

    @apply_defaults
    def __init__(self, 
                 host, 
                 remote_usr,
                 ssh_key_file,
                 remote_dir,
                 get_inputs_from=None,
                 *args, **kwargs):
        self.host = host
        self.remote_usr = remote_usr
        self.ssh_key_file = ssh_key_file
        self.remote_dir = remote_dir
        self.get_inputs_from = get_inputs_from

        log.info('--------------------RSYNCOperator ------------')
        super(RSYNCOperator, self).__init__(*args, **kwargs)

    def execute(self, context):
        log.info(context)
        log.info("###########")
        log.info("## RSYNC ##")
        log.info('Host: %s', self.host)
        log.info('User: %s', self.remote_usr)
        log.info('Remote dir: %s', self.remote_dir)
        log.info('SSH Key: %s', self.ssh_key_file)
        
        # check default XCOM key in task_id 'get_inputs_from'
        files_str = ""
        files = context['task_instance'].xcom_pull(task_ids=self.get_inputs_from, key=XCOM_RETURN_KEY)

        # stop processing if there are no products
        if files is None:
            log.info("Nothing to process.")
            return

        if isinstance(files, six.string_types):
            files_str = files
        else:
            for f in files:
                files_str += " " + f
            log.info("Retrieving input from task_id '{}'' and key '{}'".format(self.get_inputs_from, XCOM_RETURN_KEY))

        bash_command = 'rsync -avHPze "ssh -i ' + self.ssh_key_file + ' -o StrictHostKeyChecking=no" ' + files_str + ' ' + self.remote_usr + '@' + self.host + ':' + self.remote_dir
        bo = BashOperator(task_id='bash_operator_rsync_', bash_command=bash_command)
        bo.execute(context)

        # construct list of filenames uploaded to remote host
        files_list = files_str.split()
        filenames_list = list(os.path.join(self.remote_dir, os.path.basename(path)) for path in files_list)
        log.info("Uploaded files: {}".format(pprint.pformat(files_list)))
        return filenames_list

class S1MetadataOperator(BaseOperator):
    @apply_defaults
    def __init__(self, granules_paths, granules_upload_dir, working_dir, get_inputs_from=None, *args, **kwargs):
        self.granules_paths = granules_paths
        self.granules_upload_dir = granules_upload_dir
        self.working_dir = working_dir
        self.get_inputs_from = get_inputs_from

        super(S1MetadataOperator, self).__init__(*args, **kwargs)

    def execute(self, context):
        log.info('--------------------S1Metadata_PLUGIN running------------')
        task_instance = context['task_instance']

        log.info("""
            granules_paths: {}
            granules_upload_dir: {}
            get_inputs_from: {}
            """.format(
            self.granules_paths,
            self.granules_upload_dir,
            self.get_inputs_from,
            )
        )

        log.info("Receiving from 'get_input_from':\n{}".format(self.get_inputs_from))

        download_task_id = self.get_inputs_from['download_task_id']
        addo_task_ids = self.get_inputs_from['addo_task_ids']
        upload_task_ids = self.get_inputs_from['upload_task_ids']
        archive_product_task_id = self.get_inputs_from['archive_product_task_id']

        downloaded = context['task_instance'].xcom_pull(task_ids=download_task_id, key=XCOM_RETURN_KEY)
        local_granules_paths = context['task_instance'].xcom_pull(task_ids=addo_task_ids, key=XCOM_RETURN_KEY)
        uploaded_granules_paths = context['task_instance'].xcom_pull(task_ids=upload_task_ids, key=XCOM_RETURN_KEY)
        original_package_path = context['task_instance'].xcom_pull(task_ids=archive_product_task_id, key=XCOM_RETURN_KEY)

        safe_package_path = downloaded.keys()[0]
        product_id = downloaded[safe_package_path].get('title')
        working_dir = os.path.join(self.working_dir, product_id)

        log.info('safe_package_path: {}'.format(safe_package_path))
        log.info('local_granules_paths: {}'.format(local_granules_paths))

        po = PythonOperator(
            task_id="s1_metadata_dictionary_creation",
            python_callable=create_procuct_zip,
            op_kwargs={
                'sentinel1_product_zip_path': safe_package_path,
                'granules_paths': local_granules_paths,
                'granules_upload_dir':self.granules_upload_dir,
                'uploaded_granules_paths': uploaded_granules_paths,
                'original_package_path': original_package_path,
                'working_dir': working_dir
            }
        )
        zip_paths=list()
        zip_paths.append(po.execute(context))
        return zip_paths

class MoveFilesOperator(BaseOperator):
    @apply_defaults
    def __init__(self, src_dir, dst_dir, filter, *args, **kwargs):
        super(MoveFilesOperator, self).__init__(*args, **kwargs)
        self.src_dir = src_dir
        self.dst_dir = dst_dir
        self.filter = filter
        log.info('--------------------MoveFilesOperator------------')

    def execute(self, context):
        log.info('\nsrc_dir={}\ndst_dir={}\nfilter={}'.format(self.src_dir, self.dst_dir, self.filter))
        filenames = fnmatch.filter(os.listdir(self.src_dir), self.filter)
        for filename in filenames:
            filepath = os.path.join(self.src_dir, filename)
            if not os.path.exists(self.dst_dir):
                log.info("Creating directory {}".format(self.dst_dir))
                os.makedirs(self.dst_dir)
            dst_filename = os.path.join(self.dst_dir, os.path.basename(filename))
            log.info("Moving {} to {}".format(filepath, dst_filename))
            #os.rename(filepath, dst_filename)

class EVOODASPlugin(AirflowPlugin):
    name = "RSYNC_plugin"
    operators = [RSYNCOperator, ZipInspector, S1MetadataOperator, MoveFilesOperator]
