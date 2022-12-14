#!/usr/bin/env python

import datetime
import urllib.request
import json
import ssl
import traceback
import sys
import time
import logging
import os
import socket
import prometheus_client
import prometheus_client.core

# add prometheus decorators
REQUEST_TIME = prometheus_client.Summary('request_processing_seconds', 'Time spent processing request')

def get_config():
    '''Get configuration from ENV variables'''
    conf['name'] = 'pxc'
    conf['tasks'] = ['perconaxtradbclusterbackups']
    conf['keys_to_get'] = list()
    env_lists_options = ['tasks', 'labels_and_annotations_to_get', 'keys_to_get']
    for opt in env_lists_options:
        opt_val = os.environ.get(opt.upper())
        if opt_val:
            conf[opt] = opt_val.split()
    conf['url'] = 'https://kubernetes.default.svc'
    conf['log_level'] = 'INFO'
    conf['header_user_agent'] = ''
    conf['test_perconaxtradbclusterbackups'] = ''
    env_text_options = ['url', 'log_level', 'header_user_agent', 'test_perconaxtradbclusterbackups']
    for opt in env_text_options:
        opt_val = os.environ.get(opt.upper())
        if opt_val:
            conf[opt] = opt_val
    conf['check_timeout'] = 10
    conf['main_loop_sleep_interval'] = 10
    conf['listen_port'] = 9647
    env_int_options = ['check_timeout', 'main_loop_sleep_interval', 'listen_port']
    for opt in env_int_options:
        opt_val = os.environ.get(opt.upper())
        if opt_val:
            conf[opt] = int(opt_val)
    # See https://github.com/percona/percona-xtradb-cluster-operator/blob/main/pkg/apis/pxc/v1/pxc_backup_types.go#L62-L67
    conf['state_map'] = {
        'Unknown': -1,
        'Failed': 0,
        'Starting': 1,
        'Running': 2,
        'Succeeded': 3,
    }

def configure_logging():
    '''Configure logging module'''
    log = logging.getLogger(__name__)
    log.setLevel(conf['log_level'])
    FORMAT = '%(asctime)s %(levelname)s %(message)s'
    logging.basicConfig(format=FORMAT)
    return log

# Decorate function with metric.
@REQUEST_TIME.time()
def get_data():
    '''Get data from target service'''
    for task_name in conf['tasks']:
        get_data_function = globals()['get_data_'+ task_name]
        get_data_function()
                
def get_data_generic(task_name, path):
    '''Get generic json metrics via http'''
    if 'json' in conf['test_' + task_name]:
        file_name = '/opt/exporter/test/{}'.format(conf['test_' + task_name])
        log.debug('Test mode, parsing metrics from file: "{}"'.format(file_name))
        with open(file_name) as response_file:
            json_data = json.load(response_file)
            parse_data_function = globals()['parse_data_'+ task_name]
            parse_data_function(task_name, json_data)
            return
    url = conf['url'] + path
    req = urllib.request.Request(url)
    if conf['header_user_agent']:
        req.add_header('User-Agent', conf['header_user_agent'].encode())
    try:
        token = open('/var/run/secrets/kubernetes.io/serviceaccount/token').read()
        req.add_header('Authorization', 'Bearer {0}'.format(token))
        context = ssl.SSLContext()
        context.load_verify_locations(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
        response = urllib.request.urlopen(req, context=context, timeout=conf['check_timeout'])
        pxc_exporter_http_code.set(response.getcode())
        raw_data = response.read().decode()
        json_data = json.loads(raw_data)
        parse_data_function = globals()['parse_data_'+ task_name]
        parse_data_function(task_name, json_data)
    except socket.timeout:
        pxc_exporter_http_code.set(-1)
    except urllib.error.HTTPError as e:
        pxc_exporter_http_code.set(e.code)

def get_data_perconaxtradbclusterbackups():
    '''Get perconaxtradbclusterbackups json metrics via http'''
    get_data_generic('perconaxtradbclusterbackups', '/apis/pxc.percona.com/v1/perconaxtradbclusterbackups')

def parse_data_generic(task_name, json_data):
    '''Parse data from generic json'''
    for task_data in json_data['items']:
        log.debug('parse_data_{}, data: "{}"'.format(task_name, task_data))
        labels = dict()
        for key in conf['keys_to_get']:
            if key in task_data['metadata']:
                labels[key] = task_data['metadata'][key]
        #labels['pxc_cluster'] = task_data['pxcCluster']
        #labels['storage_name'] = task_data['storageName']
        metric_name = '{}_exporter_{}_state'.format(conf['name'], task_name)
        description = '{} PerconaXtraDBClusterBackup state code. Unknown=-1, Failed=0, Starting=1, Running=2, Succeeded=3'.format(task_name)
        state = task_data['status'].get('state', 'Unknown')
        value =  conf['state_map'][state]
        metric = {'metric_name': metric_name, 'labels': labels, 'description': description, 'value': value}
        data.append(metric)
        if 'completed' in task_data['status']:
            metric_name = '{}_exporter_{}_completed_timestamp'.format(conf['name'], task_name)
            description = '{} PerconaXtraDBClusterBackup completed timestamp'.format(task_name)
            value = datetime.datetime.strptime(task_data['status']['completed'][:-1], '%Y-%m-%dT%H:%M:%S').timestamp()
            metric = {'metric_name': metric_name, 'labels': labels, 'description': description, 'value': value}
            data.append(metric)

def parse_data_perconaxtradbclusterbackups(task_name, json_data):
    '''Parse data from perconaxtradbclusterbackups json'''
    parse_data_generic(task_name, json_data)

def label_clean(label):
    label = str(label)
    replace_map = {
        '\\': '',
        '"': '',
        '\n': '',
        '\t': '',
        '\r': '',
        '-': '_',
        ' ': '_'
    }
    for r in replace_map:
        label = label.replace(r, replace_map[r])
    return label

# run
conf = dict()
get_config()
log = configure_logging()
log.debug('Config: "{}"'.format(conf))
data_tmp = dict()
data = list()

pxc_exporter_up = prometheus_client.Gauge('pxc_exporter_up', 'exporter scrape status')
pxc_exporter_errors_total = prometheus_client.Counter('pxc_exporter_errors_total', 'exporter scrape errors total counter')
pxc_exporter_http_code = prometheus_client.Gauge('pxc_exporter_http_code', 'exporter scrape http code')

class Collector(object):
    def collect(self):
        # add static metrics
        gauge = prometheus_client.core.GaugeMetricFamily
        counter = prometheus_client.core.CounterMetricFamily
        # get dinamic data
        try:
            get_data()
            pxc_exporter_up.set(1)
        except:
            trace = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
            for line in trace:
                print(line[:-1], flush=True)
            pxc_exporter_up.set(0)
            pxc_exporter_errors_total.inc()
        # add dinamic metrics
        to_yield = set()
        for _ in range(len(data)):
            metric = data.pop()
            labels = list(metric['labels'].keys())
            labels_values = [ metric['labels'][k] for k in labels ]
            if metric['metric_name'] not in to_yield:
                setattr(self, metric['metric_name'], gauge(metric['metric_name'], metric['description'], labels=labels))
            if labels:
                getattr(self, metric['metric_name']).add_metric(labels_values, metric['value'])
                to_yield.add(metric['metric_name'])
        for metric in to_yield:
            yield getattr(self, metric)

registry = prometheus_client.core.REGISTRY
registry.register(Collector())

prometheus_client.start_http_server(conf['listen_port'])

# endless loop
while True:
    try:
        while True:
            time.sleep(conf['main_loop_sleep_interval'])
    except KeyboardInterrupt:
        break
    except:
        trace = traceback.format_exception(sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2])
        for line in trace:
            print(line[:-1], flush=True)

