import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import host_guard as h


class HostGuardTests(unittest.TestCase):
    def test_partial_io_preserves_iops(self):
        current=h.io_values('8:0 rbps=max wbps=max riops=7200 wiops=12600\n8:16 rbps=1','8:0')
        updates=h.desired_io(current,128,48)
        self.assertEqual(updates,{'rbps':str(128*h.MIB),'wbps':str(48*h.MIB)})
        self.assertEqual(current['riops'],'7200')
        self.assertNotIn('riops',updates)

    def test_matching_io_is_not_rewritten(self):
        self.assertEqual(h.desired_io({'rbps':str(128*h.MIB),'wbps':str(48*h.MIB)},128,48),{})

    def test_high_tracks_actual_decimal_limit(self):
        high=int(h.desired_high('16000000000',0.9))
        self.assertLessEqual(high,14400000000)
        self.assertLess(14400000000-high,4096)
        with self.assertRaises(ValueError):h.desired_high('max',0.9)

    def test_policy_rejects_paths_and_invalid_budgets(self):
        good={'instances':{'fedora-heart':{'read_mib':128,'write_mib':48,'memory_high_ratio':0.9}}}
        self.assertEqual(h.validate(good),good)
        for name,entry in (('../host',{'read_mib':1,'write_mib':1}),
                           ('guest',{'read_mib':0,'write_mib':1}),
                           ('guest',{'read_mib':128,'write_mib':48,'memory_high_ratio':1.0}),
                           ('guest',{'read_mib':True,'write_mib':48})):
            with self.subTest(name=name),self.assertRaises(ValueError):
                h.validate({'instances':{name:entry}})

    @patch.object(h,'write_control')
    def test_observation_does_not_write(self,write):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'lxc.payload.guest';p.mkdir()
            (p/'io.max').write_text('8:0 rbps=max wbps=max riops=7200 wiops=12600')
            (p/'memory.max').write_text('16000000000');(p/'memory.high').write_text('max')
            result=h.reconcile(root,'guest',{'read_mib':128,'write_mib':48,'memory_high_ratio':0.9},'8:0',False)
            self.assertIn('memory_high_update',result)
            write.assert_not_called()

    def test_stopped_instance_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(h.reconcile(Path(d),'absent',{'read_mib':1,'write_mib':1},'8:0',True)['state'],'not-running')

    def test_process_io_deltas_ignore_reset(self):
        old={'1':{'comm':'compiler','cgroup':'/guest','read':100,'write':100}}
        current={'1':{'comm':'compiler','cgroup':'/guest','read':200,'write':400}}
        self.assertEqual(h.io_deltas(old,current,10)[0]['write_bps'],30)
        current['1']['write']=0
        self.assertEqual(h.io_deltas(old,current,10),[])



class OomPriorityTests(unittest.TestCase):
    @patch.object(h,'protect',return_value={'critical':2,'build':0})
    @patch.object(h,'critical_container_groups',side_effect=h.DiscoveryError('daemon-unavailable',1))
    def test_docker_failure_preserves_cached_targets_and_still_protects(self,discover,protect):
        cached=['/system.slice/docker-existing.scope'];policy={'oom_protection':{'containers':['coolify-proxy']}}
        groups,changed,error=h.protection_step(Path('/cg'),policy,cached,True,True)
        self.assertEqual(groups,cached);self.assertEqual(changed['critical'],2)
        self.assertEqual(error['reason'],'daemon-unavailable')
        protect.assert_called_once_with(Path('/cg'),policy,cached,True)

    @patch.object(h,'protect',return_value={})
    @patch.object(h,'critical_container_groups',side_effect=h.subprocess.TimeoutExpired('docker',3))
    def test_initial_discovery_timeout_still_protects_known_guest_and_units(self,discover,protect):
        policy={'oom_protection':{'instances':['fedora-musecat'],'units':['incus.service']}}
        groups,_,error=h.protection_step(Path('/cg'),policy,[],True,True)
        self.assertEqual(groups,[]);self.assertEqual(error['error'],'TimeoutExpired')
        protect.assert_called_once_with(Path('/cg'),policy,[],True)

    @patch.object(h.subprocess,'run')
    def test_discovery_logs_only_fixed_reason_and_exit_code(self,run):
        run.return_value=h.subprocess.CompletedProcess([],1,'','Cannot connect to daemon: secret-url')
        with self.assertRaises(h.DiscoveryError) as raised:h.critical_container_groups(['coolify-proxy'])
        self.assertEqual(raised.exception.reason,'daemon-unavailable')
        self.assertNotIn('secret',str(raised.exception))

    def test_critical_is_last_resort_and_existing_immunity_preserved(self):
        self.assertEqual(h.priority(0,True,False),-950)
        self.assertEqual(h.priority(-1000,True,False),-1000)
        self.assertEqual(h.priority(100,True,True),-950)

    def test_builds_go_first_but_other_services_keep_existing_score(self):
        self.assertEqual(h.priority(0,False,True),500)
        self.assertEqual(h.priority(800,False,True),800)
        self.assertEqual(h.priority(-1000,False,True),-1000)
        self.assertEqual(h.priority(-500,False,False),-500)

    def test_only_known_build_processes_are_promoted(self):
        self.assertTrue(h.is_build('clang++'))
        self.assertTrue(h.is_build('rustc'))
        self.assertTrue(h.is_build('java',b'java\0org.gradle.launcher.daemon.bootstrap.GradleDaemon\0'))
        self.assertFalse(h.is_build('java',b'java\0production-server.jar\0'))
        self.assertFalse(h.is_build('pocketbase-serv'))
        self.assertFalse(h.is_build('caddy'))

    def test_protected_cgroup_membership_requires_component_boundary(self):
        group='/lxc.payload.fedora-musecat'
        self.assertTrue(h.in_group(group,group))
        self.assertTrue(h.in_group(group+'/system.slice/app.service',group))
        self.assertFalse(h.in_group(group+'-other/system.slice',group))

    def test_policy_does_not_allow_unconditional_kernel_immunity(self):
        with self.assertRaises(ValueError):
            h.validate({'instances':{},'oom_protection':{'critical_score_adj':-1000}})


if __name__=='__main__':unittest.main()
