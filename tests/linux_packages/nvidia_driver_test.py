# Copyright 2016 PerfKitBenchmarker Authors. All rights reserved.
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
"""Tests for perfkitbenchmarker.packages.nvidia_driver."""

import os
import unittest
import mock

from perfkitbenchmarker import test_util
from perfkitbenchmarker.linux_packages import nvidia_driver


AUTOBOOST_ENABLED_DICT = {'autoboost': True, 'autoboost_default': True}
AUTOBOOST_DISABLED_DICT = {'autoboost': False, 'autoboost_default': False}


class NvidiaDriverTestCase(unittest.TestCase, test_util.SamplesTestMixin):

  def setUp(self):
    super(NvidiaDriverTestCase, self).setUp()
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'nvidia_smi_output.txt')
    with open(path) as fp:
      self.nvidia_smi_output = fp.read()

  def testQueryNumberOfGpus(self):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(return_value=('count\n8', None))
    self.assertEqual(8, nvidia_driver.QueryNumberOfGpus(vm))

  def testQueryGpuClockSpeed(self):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=('clocks.applications.graphics [MHz], '
                      'clocks.applications.memory [Mhz]\n'
                      '324 MHz, 527 MHz', None))
    self.assertEqual((324, 527), nvidia_driver.QueryGpuClockSpeed(vm, 3))
    vm.RemoteCommand.assert_called_with(
        'sudo nvidia-smi '
        '--query-gpu=clocks.applications.memory,'
        'clocks.applications.graphics --format=csv --id=3')

  def testGetDriverVersion(self):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(self.nvidia_smi_output, ''))
    self.assertEqual('375.66', nvidia_driver.GetDriverVersion(vm))

  def testGetPeerToPeerTopology(self):
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'nvidia_smi_topo_output.txt')
    with open(path) as fp:
      nvidia_smi_output = fp.read()

    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(nvidia_smi_output, ''))

    expected = 'Y Y N N;Y Y N N;N N Y Y;N N Y Y'
    actual = nvidia_driver.GetPeerToPeerTopology(vm)
    self.assertEqual(expected, actual)
    vm.RemoteCommand.assert_called_with('nvidia-smi topo -p2p r')

  def testQueryAutoboostNull(self):
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'nvidia_smi_describe_clocks_p100.txt')
    with open(path) as fp:
      nvidia_smi_output = fp.read()
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(nvidia_smi_output, ''))
    self.assertEqual({'autoboost': None, 'autoboost_default': None},
                     nvidia_driver.QueryAutoboostPolicy(vm, 0))

  def testQueryAutoboostOn(self):
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'nvidia_smi_describe_clocks_k80.txt')
    with open(path) as fp:
      nvidia_smi_output = fp.read()
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(nvidia_smi_output, ''))
    self.assertEqual({'autoboost': False, 'autoboost_default': True},
                     nvidia_driver.QueryAutoboostPolicy(vm, 0))

  def testGetGpuTypeP100(self):
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'list_gpus_output_p100.txt')
    with open(path) as fp:
      nvidia_smi_output = fp.read()
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(nvidia_smi_output, ''))
    self.assertEqual(nvidia_driver.NVIDIA_TESLA_P100,
                     nvidia_driver.GetGpuType(vm))

  def testGetGpuTypeK80(self):
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'list_gpus_output_k80.txt')
    with open(path) as fp:
      nvidia_smi_output = fp.read()
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(nvidia_smi_output, ''))
    self.assertEqual(nvidia_driver.NVIDIA_TESLA_K80,
                     nvidia_driver.GetGpuType(vm))

  def testHetergeneousGpuTypes(self):
    path = os.path.join(os.path.dirname(__file__), '../data',
                        'list_gpus_output_heterogeneous.txt')
    with open(path) as fp:
      nvidia_smi_output = fp.read()
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock(
        return_value=(nvidia_smi_output, ''))
    self.assertRaisesRegexp(nvidia_driver.HeterogeneousGpuTypesError,  # pytype: disable=wrong-arg-count
                            'PKB only supports one type of gpu per VM',
                            nvidia_driver.GetGpuType, vm)

  @mock.patch(nvidia_driver.__name__ + '.QueryNumberOfGpus', return_value=2)
  @mock.patch(nvidia_driver.__name__ + '.QueryAutoboostPolicy',
              return_value=AUTOBOOST_ENABLED_DICT)
  def testSetAutoboostPolicyWhenValuesAreTheSame(self,
                                                 query_autoboost_mock,
                                                 num_gpus_mock):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock()

    nvidia_driver.SetAutoboostDefaultPolicy(vm, True)
    query_autoboost_mock.assetCalled()
    vm.RemoteCommand.assert_not_called()

  @mock.patch(nvidia_driver.__name__ + '.QueryNumberOfGpus', return_value=2)
  @mock.patch(nvidia_driver.__name__ + '.QueryAutoboostPolicy',
              return_value=AUTOBOOST_DISABLED_DICT)
  def testSetAutoboostPolicyWhenValuesAreDifferent(self,
                                                   query_autoboost_mock,
                                                   num_gpus_mock):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock()

    nvidia_driver.SetAutoboostDefaultPolicy(vm, True)
    query_autoboost_mock.assetCalled()
    self.assertEqual(2, vm.RemoteCommand.call_count)

  @mock.patch(nvidia_driver.__name__ + '.QueryNumberOfGpus', return_value=2)
  @mock.patch(nvidia_driver.__name__ + '.QueryGpuClockSpeed',
              return_value=(2505, 875))
  def testSetClockSpeedWhenValuesAreTheSame(self,
                                            query_clock_speed_mock,
                                            num_gpus_mock):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock()

    nvidia_driver.SetGpuClockSpeed(vm, 2505, 875)
    query_clock_speed_mock.assetCalled()
    vm.RemoteCommand.assert_not_called()

  @mock.patch(nvidia_driver.__name__ + '.QueryNumberOfGpus', return_value=2)
  @mock.patch(nvidia_driver.__name__ + '.QueryGpuClockSpeed',
              return_value=(2505, 875))
  def testSetClockSpeedWhenValuesAreDifferent(self,
                                              query_clock_speed_mock,
                                              num_gpus_mock):
    vm = mock.MagicMock()
    vm.RemoteCommand = mock.MagicMock()

    nvidia_driver.SetGpuClockSpeed(vm, 2505, 562)
    query_clock_speed_mock.assetCalled()
    self.assertEqual(2, vm.RemoteCommand.call_count)

if __name__ == '__main__':
  unittest.main()
