# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Module containing aerospike server installation and cleanup functions."""

import logging
from absl import flags
from perfkitbenchmarker import data
from perfkitbenchmarker import errors
from perfkitbenchmarker import linux_packages
from perfkitbenchmarker import vm_util

FLAGS = flags.FLAGS

GIT_REPO = 'https://github.com/aerospike/aerospike-server.git'
GIT_TAG = '6.0.0.2'
AEROSPIKE_DIR = '%s/aerospike-server' % linux_packages.INSTALL_DIR

MEMORY = 'memory'
DISK = 'disk'
flags.DEFINE_enum('aerospike_storage_type', MEMORY, [MEMORY, DISK],
                  'The type of storage to use for Aerospike data. The type of '
                  'disk is controlled by the "data_disk_type" flag.')
flags.DEFINE_integer('aerospike_replication_factor', 1,
                     'Replication factor for aerospike server.')
flags.DEFINE_integer('aerospike_service_threads', 4,
                     'Number of threads per transaction queue.')
flags.DEFINE_integer('aerospike_vms', 1,
                     'Number of vms (nodes) for aerospike server.')

MIN_FREE_KBYTES = 1160000


def _GetAerospikeDir(idx=None):
  if idx is None:
    return f'{linux_packages.INSTALL_DIR}/aerospike-server'
  else:
    return f'{linux_packages.INSTALL_DIR}/{idx}/aerospike-server'


def _GetAerospikeConfig(idx=None):
  return f'{_GetAerospikeDir(idx)}/as/etc/aerospike_dev.conf'


def _Install(vm):
  """Installs the Aerospike server on the VM."""
  vm.Install('build_tools')
  vm.Install('lua5_1')
  vm.Install('openssl')
  vm.RemoteCommand('git clone {0} {1}'.format(GIT_REPO, _GetAerospikeDir()))
  # Comment out Werror flag and compile. With newer compilers gcc7xx,
  # compilation is broken due to warnings.
  vm.RemoteCommand(
      'cd {0} && git checkout {1} && git submodule update --init '
      '&& sed -i "s/COMMON_CFLAGS += -Werror/# $COMMON_CFLAGS += -Werror/" '
      '{0}/make_in/Makefile.in '
      '&& make'.format(_GetAerospikeDir(), GIT_TAG))
  for idx in range(FLAGS.aerospike_instances):
    vm.RemoteCommand(f'mkdir {linux_packages.INSTALL_DIR}/{idx}; '
                     f'cp -rf {_GetAerospikeDir()} {_GetAerospikeDir(idx)}')


def YumInstall(vm):
  """Installs the aerospike_server package on the VM."""
  _Install(vm)


def AptInstall(vm):
  """Installs the aerospike_server package on the VM."""
  vm.InstallPackages('netcat-openbsd zlib1g-dev')
  _Install(vm)


@vm_util.Retry(poll_interval=5, timeout=300,
               retryable_exceptions=(errors.Resource.RetryableCreationError))
def _WaitForServerUp(server, idx=None):
  """Block until the Aerospike server is up and responsive.

  Will timeout after 5 minutes, and raise an exception. Before the timeout
  expires any exceptions are caught and the status check is retried.

  We check the status of the server by connecting to Aerospike's out
  of band telnet management port and issue a 'status' command. This should
  return 'ok' if the server is ready. Per the aerospike docs, this always
  returns 'ok', i.e. if the server is not up the connection will fail or we
  would get no response at all.

  Args:
    server: VirtualMachine Aerospike has been installed on.
    idx: aerospike process index.

  Raises:
    errors.Resource.RetryableCreationError when response is not 'ok' or if there
      is an error connecting to the telnet port or otherwise running the remote
      check command.
  """
  address = server.internal_ip
  port = f'{idx + 3}003'

  logging.info('Trying to connect to Aerospike at %s:%s', server, port)
  try:
    def _NetcatPrefix():
      _, stderr = server.RemoteCommand('nc -h', ignore_failure=True)
      if '-q' in stderr:
        return 'nc -q 1'
      else:
        return 'nc -i 1'

    out, _ = server.RemoteCommand(
        '(echo -e "status\n" ; sleep 1)| %s %s %s' % (
            _NetcatPrefix(), address, port))
    if out.startswith('ok'):
      logging.info('Aerospike server status is OK. Server up and running.')
      return
  except errors.VirtualMachine.RemoteCommandError as e:
    raise errors.Resource.RetryableCreationError(
        'Aerospike server not up yet: %s.' % str(e))
  else:
    raise errors.Resource.RetryableCreationError(
        "Aerospike server not up yet. Expected 'ok' but got '%s'." % out)


def ConfigureAndStart(server, seed_node_ips=None):
  """Prepare the Aerospike server on a VM.

  Args:
    server: VirtualMachine to install and start Aerospike on.
    seed_node_ips: internal IP addresses of seed nodes in the cluster.
      Leave unspecified for a single-node deployment.
  """
  server.Install('aerospike_server')
  seed_node_ips = seed_node_ips or [server.internal_ip]

  if FLAGS.aerospike_storage_type == DISK:
    for scratch_disk in server.scratch_disks:
      if scratch_disk.mount_point:
        server.RemoteCommand(
            f'sudo umount {scratch_disk.mount_point}', ignore_failure=True)

    devices = [scratch_disk.GetDevicePath()
               for scratch_disk in server.scratch_disks]

    # https://docs.aerospike.com/server/operations/plan/ssd/ssd_init
    # Expect to exit 1 with `No space left on device` error.
    def _WipeDevice(device):
      server.RobustRemoteCommand(
          f'sudo dd if=/dev/zero of={device} bs=1M', ignore_failure=True)

    vm_util.RunThreaded(_WipeDevice, devices)

    @vm_util.Retry(
        poll_interval=5,
        timeout=300,
        retryable_exceptions=(errors.Resource.RetryableCreationError))
    def _ZeroizeHeader(device):
      try:
        server.RemoteCommand(f'sudo sudo blkdiscard -z --length 8MiB {device}')
      except errors.Resource.RetryableCreationError as e:
        raise errors.VirtualMachine.RemoteCommandError(
            f'Device {device} header not zeroized: {e}.')

    for device in devices:
      _ZeroizeHeader(device)

  else:
    devices = []

  # Linux best practice based on:
  # https://docs.aerospike.com/server/operations/install/linux/bestpractices#linux-best-practices
  server.RemoteCommand(f'echo {MIN_FREE_KBYTES * FLAGS.aerospike_instances} '
                       '| sudo tee /proc/sys/vm/min_free_kbytes')
  server.RemoteCommand('echo 0 | sudo tee /proc/sys/vm/swappiness')
  for idx in range(FLAGS.aerospike_instances):
    current_devices = []
    if devices:
      num_device_per_instance = int(len(devices) / FLAGS.aerospike_instances)
      current_devices = devices[idx * num_device_per_instance:(idx + 1) *
                                num_device_per_instance]
    server.RenderTemplate(
        data.ResourcePath('aerospike.conf.j2'), _GetAerospikeConfig(idx), {
            'devices':
                current_devices,
            'port_prefix':
                3 + idx,
            'memory_size':
                int(server.total_memory_kb * 0.8 / FLAGS.aerospike_instances),
            'seed_addresses':
                seed_node_ips,
            'service_threads':
                FLAGS.aerospike_service_threads,
            'replication_factor':
                FLAGS.aerospike_replication_factor,
        })

    server.RemoteCommand(f'cd {_GetAerospikeDir(idx)} && make init')
    # Persist the nohup command past the ssh session
    # "sh -c 'cd /whereever; nohup ./whatever > /dev/null 2>&1 &'"
    log_file = f'~/aerospike-{server.name}-{idx}.log'
    cmd = (f'sh -c \'cd {_GetAerospikeDir(idx)} && nohup sudo make start > '
           f'{log_file} 2>&1 &\'')
    server.RemoteCommand(cmd)
    server.PullFile(vm_util.GetTempDir(), log_file)
    _WaitForServerUp(server, idx)
    logging.info('Aerospike server configured and started.')


def Uninstall(vm):
  del vm
