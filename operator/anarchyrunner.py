import copy
import json
import kubernetes
import logging
import os
import requests
import requests.auth
import tempfile
import threading
import time

from anarchyutil import deep_update, random_string

operator_logger = logging.getLogger('operator')

class AnarchyRunner(object):
    """Represents a pool of runner pods to process for AnarchyGovernors"""

    cache = {}

    @staticmethod
    def default_runner_definition(runtime):
        return {
            'apiVersion': runtime.operator_domain + '/v1',
            'kind': 'AnarchyRunner',
            'metadata': {
                'name': 'default',
                'resourceVersion': '0',
                'ownerReferences': [{
                    'apiVersion': runtime.anarchy_service.api_version,
                    'controller': True,
                    'kind': runtime.anarchy_service.kind,
                    'name': runtime.anarchy_service.metadata.name,
                    'uid': runtime.anarchy_service.metadata.uid
                }]
            },
            'spec': {
                'minReplicas': 1,
                'maxReplicas': 9,
                'token': random_string(50),
                'podTemplate': {
                    'spec': {
                        'serviceAccountName': 'anarchy-runner-default',
                        'containers': [{
                            'name': 'runner',
                            'resources': {
                                'limits': {
                                    'cpu': '1',
                                    'memory': '256Mi',
                                },
                                'requests': {
                                    'cpu': '500m',
                                    'memory': '256Mi',
                                },
                            },
                        }]
                    }
                }
            }
        }

    @staticmethod
    def get(name):
        return AnarchyRunner.cache.get(name, None)

    @staticmethod
    def init(runtime):
        '''
        Get initial list of AnarchyRunners.

        This method is used during start-up to ensure that all AnarchyRunner definitions are
        loaded before processing starts.
        '''
        for resource in runtime.custom_objects_api.list_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyrunners'
        ).get('items', []):
            AnarchyRunner.register(resource)

        for pod in runtime.core_v1_api.list_namespaced_pod(
            runtime.operator_namespace, label_selector=runtime.runner_label
        ).items:
            runner_name = pod.metadata.labels[runtime.runner_label]
            runner = AnarchyRunner.get(runner_name)
            if runner:
                runner.pods[pod.metadata.name] = pod
            else:
                operator_logger.warning("Init found runner pod %s but no runner named %s", pod.metadata.name, runner_name)

    @staticmethod
    def manage_runners(runtime):
        for runner in AnarchyRunner.cache.values():
            runner.manage(runtime)

    @staticmethod
    def register(resource):
        name = resource['metadata']['name']
        runner = AnarchyRunner.cache.get(name)
        if runner:
            runner.refresh_from_resource(resource)
        else:
            runner = AnarchyRunner(resource)
            AnarchyRunner.cache[name] = runner
            operator_logger.info("Registered runner %s", runner.name)
        return runner

    @staticmethod
    def unregister(runner):
        AnarchyRunner.cache.pop(runner.name if isinstance(runner, AnarchyRunner) else runner, None)

    @staticmethod
    def watch(runtime):
        '''
        Watch AnarchyRunners and keep definitions synchronized

        This watch is independent of the kopf watch and is used to keep runner definitions updated
        even when the pod is not the active peer.
        '''
        for event in kubernetes.watch.Watch().stream(
            runtime.custom_objects_api.list_namespaced_custom_object,
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchyrunners'
        ):
            obj = event.get('object')
            if event['type'] == 'DELETED':
                AnarchyRunner.unregister(obj['metadata']['name'])
            elif obj \
            and obj.get('apiVersion') == runtime.operator_domain + '/v1' \
            and obj.get('kind') == 'AnarchyRunner':
                AnarchyRunner.register(obj)

    @staticmethod
    def watch_pods(runtime):
        '''
        Watch Pods with Anarchy runner label and keep list up to date
        '''
        for event in kubernetes.watch.Watch().stream(
            runtime.core_v1_api.list_namespaced_pod,
            runtime.operator_namespace, label_selector=runtime.runner_label
        ):
            pod = event.get('object')
            if pod and isinstance(pod, kubernetes.client.V1Pod) \
            and pod.metadata.labels:
                runner_name = pod.metadata.labels[runtime.runner_label]
                if not runner_name:
                    continue
                if event['type'] == 'DELETED':
                    runner = AnarchyRunner.get(runner_name)
                    if runner:
                        runner.pods.pop(pod.metadata.name, None)
                else:
                    runner = AnarchyRunner.get(runner_name)
                    if runner:
                        operator_logger.debug('Update AnarchyRunner %s Pod %s', runner.name, pod.metadata.name)
                        runner.pods[pod.metadata.name] = pod
                    else:
                        operator_logger.warning("Watch found runner pod %s but no runner named %s", pod.metadata.name, runner_name)

    def __init__(self, resource):
        self.metadata = resource['metadata']
        self.pods = {}
        self.spec = resource['spec']
        if not self.spec.get('token'):
            self.spec['token'] = random_string(50)
        self.lock = threading.Lock()
        self.sanity_check()

    def sanity_check(self):
        pass

    @property
    def image_pull_policy(self):
        return self.spec.get('imagePullPolicy', os.environ.get('RUNNER_IMAGE_PULL_POLICY', 'Always'))

    @property
    def kind(self):
        return 'AnarchyRunner'

    @property
    def max_replicas(self):
        return self.spec.get('maxReplicas', self.min_replicas)

    @property
    def min_replicas(self):
        return self.spec.get('minReplicas', 1)

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def pod_namespace(self):
        return self.spec.get('podTemplate', {}).get('metadata', {}).get('namespace', None)

    @property
    def pod_template(self):
        return self.spec.get('podTemplate', {})

    @property
    def resources(self):
        return self.spec.get('resources', {
            'limits': { 'cpu': '1', 'memory': '256Mi' },
            'requests': { 'cpu': '200m', 'memory': '256Mi' },
        })

    @property
    def runner_token(self):
        '''
        Return runner token, used to authenticate callbacks.
        Default to use object uid if token is not set.
        '''
        return self.spec['token']

    @property
    def uid(self):
        return self.metadata.get('uid')

    def manage(self, runtime):
        '''
        Manage Pods for AnarchyRunner
        '''
        if runtime.running_all_in_one:
            return

        with self.lock:
            # Make sure the runner service account exists
            self.manage_runner_service_account(runtime)
            self.manage_runner_pods(runtime)

    def manage_runner_pods(self, runtime):
        '''
        Manage Pods for AnarchyRunner
        '''

        #deployment_name = 'anarchy-runner-' + self.name
        #deployment_namespace = self.pod_namespace or runtime.operator_namespace

        pod_template = copy.deepcopy(self.pod_template)
        if 'metadata' not in pod_template:
            pod_template['metadata'] = {}
        if 'labels' not in pod_template['metadata']:
            pod_template['metadata']['labels'] = {}
        if 'spec' not in pod_template:
            pod_template['spec'] = {}
        if 'serviceAccountName' not in pod_template['spec']:
            pod_template['spec']['serviceAccountName'] = self.service_account_name(runtime)
        if not 'containers' in pod_template['spec']:
            pod_template['spec']['containers'] = [{}]
        pod_template['metadata']['generateName'] = '{}-runner-{}-'.format(runtime.anarchy_service_name, self.name)
        pod_template['metadata']['labels'][runtime.runner_label] = self.name
        pod_template['metadata']['ownerReferences'] = [{
            'apiVersion': runtime.operator_domain + '/v1',
            'controller': True,
            'kind': 'AnarchyRunner',
            'name': self.name,
            'uid': self.uid,
        }]

        runner_container = pod_template['spec']['containers'][0]
        if 'name' not in runner_container:
            runner_container['name'] = 'runner'
        if not runner_container.get('image'):
            image = os.environ.get('RUNNER_IMAGE', '')
            if image != '':
                runner_container['image'] = image
            else:
                runner_container['image'] = runtime.pod.spec.containers[0].image
        if not 'env' in runner_container:
            runner_container['env'] = []
        runner_container['env'].extend([
            {
                'name': 'ANARCHY_COMPONENT',
                'value': 'runner'
            },{
                'name': 'ANARCHY_URL',
                'value': 'http://{}.{}.svc:5000'.format(
                    runtime.anarchy_service_name, runtime.operator_namespace
                )
            },{
                'name': 'ANARCHY_DOMAIN',
                'value': runtime.operator_domain
            },{
                'name': 'POD_NAME',
                'valueFrom': {
                    'fieldRef': {
                        'apiVersion': 'v1',
                        'fieldPath': 'metadata.name'
                    }
                }
            },{
                'name': 'RUNNER_NAME',
                'value': self.name
            },{
                'name': 'RUNNER_TOKEN',
                'value': self.runner_token
            }
        ])

        pod_count = 0
        for name, pod in self.pods.items():
            pod_dict = runtime.api_client.sanitize_for_serialization(pod)
            if pod.metadata.labels.get(runtime.runner_terminating_label) == 'true':
                # Ignore pod marked for termination
                pass
            elif pod_dict == deep_update(copy.deepcopy(pod_dict), pod_template):
                pod_count += 1
            else:
                # Pod does not match template, need to terminate pod
                runtime.core_v1_api.patch_namespaced_pod(
                    pod.metadata.name, pod.metadata.namespace,
                    { 'metadata': { 'labels': { runtime.runner_terminating_label: 'true' } } }
                )
                operator_logger.info('Labeled AnarchyRunner %s runner pod %s for termination', self.name, pod.metadata.name)

        for i in range(self.min_replicas - pod_count):
            pod = None
            while pod == None:
                try:
                    pod = runtime.core_v1_api.create_namespaced_pod(runtime.operator_namespace, pod_template)
                    break
                except kubernetes.client.rest.ApiException as e:
                    if 'retry after the token is automatically created' in json.loads(e.body).get('message', ''):
                        time.sleep(1)
                    else:
                        raise
            operator_logger.info("Started runner pod %s for AnarchyRunner %s", pod.metadata.name, self.name)

    def manage_runner_service_account(self, runtime):
        """Create service account if not found"""
        name = self.service_account_name(runtime)
        namespace = self.pod_namespace or runtime.operator_namespace
        try:
            runtime.core_v1_api.read_namespaced_service_account(name, namespace)
            return
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise
        runtime.core_v1_api.create_namespaced_service_account(
            namespace,
            kubernetes.client.V1ServiceAccount(
               metadata=kubernetes.client.V1ObjectMeta(name=name)
            )
        )

    def refresh_from_resource(self, resource):
        self.metadata = resource['metadata']
        self.spec = resource['spec']

    def service_account_name(self, runtime):
        return self.spec.get('podTemplate', {}).get('spec', {}).get('serviceAccountName', runtime.anarchy_service_name + '-runner-' + self.name)

