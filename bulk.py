# @author rithik https://github.com/rithik
# @author quinnciccoretti
# Produced in the course of work at Leidos inc, open-sourced
# because we hope its useful


# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>

import os
import subprocess
import fire
import uuid
from boto3 import session
from botocore.exceptions import ClientError
import logging
import json
import datetime
from time import sleep
import pytz
import sys
import ast
import shutil

logging.basicConfig(
    format='%(asctime).19s|%(levelname).5s: %(message)s',
    level=logging.WARNING)
log = logging.getLogger('bulk')
log.setLevel(logging.DEBUG)
TZ = pytz.timezone('US/Eastern')
# path to a folder containing deployments and
# functions subdirectories
RELATIVE_ABS_GG_LAMBDAFUNCTIONS_DIR = "../../aws-greengrass-lambda-functions/"
# relative path to provisioner jar, INCLUDING NAME OF JAR FILE!
RELATIVE_PROVISIONER_JAR_PATH = "../../aws-greengrass-provisioner/build/libs/AwsGreengrassProvisioner.jar"
# convert to absolute path to avoid headache
ABS_PROVISIONER_JAR_PATH = os.path.abspath(RELATIVE_PROVISIONER_JAR_PATH).replace("\\", "/")
ABS_GG_LAMBDAFUNCTIONS_DIR = os.path.abspath(RELATIVE_ABS_GG_LAMBDAFUNCTIONS_DIR).replace("\\", "/")

class Bulk(object):
    def __init__(self):
        # create a boto3 sessions
        s = session.Session()
        # initilize clients for the services we'll use
        self._gg = s.client("greengrass")
        self._iot = s.client("iot")
        self._iot_data = s.client("iot-data")
        self._lambda = s.client("lambda")

    def update_lambda(self, lambda_config, force):
        deployment_path = os.path.join(ABS_GG_LAMBDAFUNCTIONS_DIR, "deployments", lambda_config)
        with open(deployment_path, "r") as f:
            conf_file = f.read()
        function_names = conf_file[conf_file.find("["):conf_file.rfind("]") + 1]
        function_names = ast.literal_eval(function_names)
        for fn in function_names:
            function_conf_path = os.path.join(ABS_GG_LAMBDAFUNCTIONS_DIR, "functions", fn, "function.conf")
            function_folder = os.path.join(ABS_GG_LAMBDAFUNCTIONS_DIR, "functions", fn).replace("\\", "/")
            if os.path.exists(os.path.join(function_folder, "tsconfig.json")):
                subprocess.call([function_folder + "/node_modules/.bin/tsc"], cwd=function_folder)
                print("Typescript compiled!")
            with open(function_conf_path, "r") as f:
                function_conf_file_read = f.read()
            full_arr = function_conf_file_read.split("\n")
            if "forcePush" in function_conf_file_read:
                for k in range (0, len(full_arr)):
                    if "forcePush" in full_arr[k]:
                        del full_arr[k]
                        break
            if force:
                full_arr.insert(len(full_arr)-2, "  forcePush = true")
            else:
                full_arr.insert(len(full_arr)-2, "  forcePush = false")
            f = open(function_conf_path, "w")
            f.write('\n'.join(full_arr))
            f.close()
        return deployment_path

    # get the device shadow. This is a pure iot method, but local shadow sync is managed by greengrass
    def get_shadow(self, group_name):
        try:
            shadow_str = self._iot_data.get_thing_shadow(thingName="{}_Core".format(group_name))["payload"].read().decode("utf-8")
        except:
            shadow_str = '{}'
        return json.dumps(json.loads(shadow_str), indent=4)

    # take in a json object (not a string) and print with indents
    def pretty_print(self, json_object):
        print(json.dumps(json_object, indent=4))

    # Remove all of the HTTP stuff from the response
    def rinse(self, boto_response):
        if 'ResponseMetadata' in boto_response:
            boto_response.pop('ResponseMetadata')
        return boto_response

    # deploy all groups in your AWS account. meant to trigger lamba update
    def deploy_all(self):
        # get list of groups w/o metadata
        all_groups = self._gg.list_groups()['Groups']
        self.pretty_print(all_groups)
        for group_info in all_groups:
            self.deploy_group(group_info)

    # create a deployment for a single group
    def deploy_group(self, group_info):
        log.info("Deploying group '{0}'".format(group_info['Name']))

        # Start out by creating a deployment by getting the group id and the version id
        # DeploymentType is set to NewDeployment as default since we are not modified an
        # existing dpeloyment
        deployment = self._gg.create_deployment(
            GroupId=group_info['Id'],
            GroupVersionId=group_info['LatestVersion'],
            DeploymentType="NewDeployment")
        deployment_response = self.rinse(deployment)

        # This loop will display the status of the current deployment to the terminal
        DEPLOY_TRIES = 5
        for _ in range(DEPLOY_TRIES):
            sleep(2)
            # every two seconds check what the deployment status is
            deployment_status = self._gg.get_deployment_status(
                GroupId=group_info['Id'],
                DeploymentId=deployment_response['DeploymentId'])

            status = deployment_status.get('DeploymentStatus')

            log.debug("--- deploying... status: {0}".format(status))
            # Known status values: ['Building | InProgress | Success | Failure']
            if status == 'Success':
                log.info("--- SUCCESS!")
                return
            elif status == 'Failure':
                log.error("--- ERROR! {0}".format(deployment_status['ErrorMessage']))
                return
        # If the deployment is not complete by the deploy timeout, then quit. Something probably went wrong.
        log.warning(
            "--- Gave up waiting for deployment of group {0}. Please check the status later. "
            "Make sure GreenGrass Core is running, connected to network, "
            "and the certificates match.".format(group_info['Name']))

    # get group info from name with sad looping
    # constant time is impossible as aws does not index these things
    def get_group_info(self, group_name):
        all_groups = self._gg.list_groups()["Groups"]
        my_group_info = None
        for group_info in all_groups:
            if group_info["Name"] == group_name:
                my_group_info = group_info
                break
        if my_group_info == None:
            raise KeyError('A group of this name was not found')
        return my_group_info

    def list_core_definitions(self):
        all_core_definitions = self._gg.list_core_definitions()["Definitions"]
        for core_def in all_core_definitions:
            if "Name" in core_def:
                print(core_def["Name"])
    # get core def from group name with sad looping
    # constant time is impossible as aws does not index these things
    def get_core_definition(self, group_name):
        my_core_def = None
        all_core_definitions = self._gg.list_core_definitions()["Definitions"]
        for core_def in all_core_definitions:
            try:
                if core_def["Name"] == group_name + "_Core_Definition":
                    my_core_def = core_def
                    log.info("Core definition found")
                    break
            except:
                continue
        if my_core_def == None:
            raise KeyError('A core def of this name was not found')
        return my_core_def

    # remove a group and all associate resources. DANGER!
    # this will delete EVERYTHING you made
    def delete_group(self, group_name):
        try:
            core_def = self.get_core_definition(group_name)
            core_def_id = core_def['Id']
            self._gg.delete_core_definition(CoreDefinitionId=core_def_id)
            log.info("Core definition Deleted")
        except KeyError:
            log.warn("Cannot delete core definition, it may not exist")

        # if you didn't use provisioner, this will not work
        core_name = group_name + "_Core"
        core_policy_name = core_name + "_Policy"

        try:
            # use voodoo magic to make everything disappear
            all_principals = self._iot.list_thing_principals(thingName=core_name)["principals"]
            for principal_arn in all_principals:
                # why does aws make me do this
                # have to get id from the arn
                cert_id = principal_arn.split("cert/")[1]
                log.info("Detaching certs and policies...")

                log.info("\tdetaching policy")
                self._iot.detach_policy(target=principal_arn, policyName=core_policy_name)

                log.info("\tdetaching thing principal")
                self._iot.detach_thing_principal(thingName=core_name, principal=principal_arn)

                log.info("\tinactivating cert")
                self._iot.update_certificate(
                    certificateId=cert_id,
                    newStatus='INACTIVE'
                )

                log.info("\tdeleting cert")
                self._iot.delete_certificate(
                    certificateId=cert_id,
                    forceDelete=True
                )

                log.info("\tdeleting policy")
                self._iot.delete_policy(policyName=core_policy_name)

        except:
            log.warn("Cannot detach nor delete policies or certificates. ")

        try:
            # delete the thing
            self._iot.delete_thing(thingName=core_name)
            log.info("Core thing Deleted")
        except:
            log.warn("Cannot delete AWS Thing. It may not exist.")

        try:
            # get group information
            group_info = self.get_group_info(group_name)
            group_id = group_info['Id']
            # force reset deployments, so that we may delete
            self._gg.reset_deployments(GroupId=group_id, Force=True)
            log.info("Group Deployments Reset")
            # delete it from existence
            delete_group = self._gg.delete_group(GroupId=group_id)
            log.info(delete_group)
            log.info("Group Deleted")

        except KeyError:
            log.warn("Cannot delete group, it may not exist")

def main():
    fire.Fire(Bulk)

if __name__ == '__main__':
    log.info("Use this with the provisioner to manage groups.")
    main()
