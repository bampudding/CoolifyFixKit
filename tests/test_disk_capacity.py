import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import disk_capacity as c
import discord_notify as d

class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.credentials=self.root/'credentials'
        self.delivery=d.Delivery(self.credentials,self.root/'state');self.addCleanup(self.delivery.lock.close)
        self.policy=c.DiskCapacity(self.delivery)

    def snapshot(self,percent):
        return {'path':'/','identity':'volume','total':100*c.GIB,'used':percent*c.GIB,
                'available':(100-percent)*c.GIB,'percent':percent}

    def update(self,percent,now):
        with patch.object(c,'capacity_snapshot',return_value=self.snapshot(percent)):
            self.policy.update(now)

    def test_df_reserved_blocks_and_rounding(self):
        stat=SimpleNamespace(f_frsize=1,f_bsize=1,f_blocks=1000,f_bfree=300,f_bavail=250,f_fsid=1)
        with patch.object(c.os,'statvfs',return_value=stat):
            value=c.capacity_snapshot()
        self.assertEqual(value['percent'],74) # ceil(700 / 950 * 100), not 70%.
        self.assertEqual(value['available'],250)

    def test_exact_75_percent_warns_and_success_is_persisted(self):
        self.update(74,1000);self.assertFalse(self.delivery.data['pending'])
        self.update(75,1060);self.assertEqual(len(self.delivery.data['pending']),1)
        self.assertIn('75%',self.delivery.data['pending'][0]['text'])
        self.credentials.write_text(json.dumps({'bot_token':'not-printed','recipient_id':'1'}))
        with patch.object(d,'request',side_effect=[{'id':'channel'},{'id':'message'}]):
            self.assertTrue(self.delivery.deliver())
        self.assertEqual(self.delivery.data['disk_capacity']['notified_level'],75)
        self.delivery.lock.close()
        again=d.Delivery(self.credentials,self.root/'state');self.addCleanup(again.lock.close)
        with patch.object(c,'capacity_snapshot',return_value=self.snapshot(82)):
            c.DiskCapacity(again).update(1120)
        self.assertFalse(again.data['pending'])

    def test_pending_dedup_and_escalation_replaces_old_warning(self):
        self.update(75,1000);self.update(82,1060)
        self.assertEqual(len(self.delivery.data['pending']),1)
        self.update(85,1120)
        self.assertEqual(len(self.delivery.data['pending']),1)
        self.assertTrue(self.delivery.data['pending'][0]['incident'].endswith(':85'))
        self.update(95,1180)
        self.assertIn('긴급',self.delivery.data['pending'][0]['text'])

    def test_rearms_only_after_ten_minutes_below_70_percent(self):
        self.update(80,1000)
        self.delivery.data['disk_capacity']['notified_level']=75
        self.delivery.data['pending']=[]
        self.update(69,1060);self.update(74,1660);self.update(76,1720)
        self.assertFalse(self.delivery.data['pending'])
        self.update(69,1780);self.update(69,2380);self.update(75,2440)
        self.assertEqual(len(self.delivery.data['pending']),1)

    def test_statvfs_only_once_a_minute(self):
        with patch.object(c,'capacity_snapshot',return_value=self.snapshot(74)) as read:
            self.policy.update(1000);self.policy.update(1005)
        read.assert_called_once()
