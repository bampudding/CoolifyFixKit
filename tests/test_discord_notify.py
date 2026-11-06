import json
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import discord_notify as d
from test_resource_guard import sample

class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.credentials=root/'secret.json'
        self.credentials.write_text(json.dumps({'bot_token':'never-log-this','recipient_id':'123'}))
        self.delivery=d.Delivery(self.credentials,root/'state');self.addCleanup(self.delivery.lock.close)

    def test_dedup_and_restart_persistence(self):
        e={'kind':'oom','scope':'debian','message':'OOM'}
        self.assertTrue(self.delivery.enqueue(e));self.assertFalse(self.delivery.enqueue(e))
        self.assertEqual(len(json.loads(self.delivery.path.read_text())['pending']),1)

    def test_invalid_webhooks_rejected(self):
        for e in ({'kind':'unknown'},{'kind':'oom','message':'a'*1201},{'kind':'oom','url':'https://example.com'}):
            with self.assertRaises(ValueError):self.delivery.enqueue(e)

    @patch.object(d,'request')
    def test_delivery_uses_dm_same_token_and_disables_mentions(self,request):
        request.side_effect=[{'id':'channel'},{'id':'message'}]
        self.delivery.enqueue({'kind':'test','message':'test @everyone'})
        self.assertTrue(self.delivery.deliver())
        self.assertEqual(request.call_args_list[0].args,('never-log-this','/users/@me/channels',{'recipient_id':'123'}))
        payload=request.call_args_list[1].args[2]
        self.assertEqual(payload['allowed_mentions'],{'parse':[]});self.assertTrue(payload['enforce_nonce'])
        self.assertEqual(self.delivery.data['pending'],[])
        self.assertNotIn('never-log-this',self.delivery.path.read_text())

    @patch.object(d,'request',side_effect=d.Retry(15))
    def test_failed_delivery_keeps_same_nonce_for_retry(self,request):
        self.delivery.enqueue({'kind':'oom','message':'OOM'})
        nonce=self.delivery.data['pending'][0]['nonce']
        self.assertFalse(self.delivery.deliver());self.assertEqual(self.delivery.data['pending'][0]['nonce'],nonce)
        self.assertFalse(self.delivery.deliver());self.assertEqual(request.call_count,1)

    def test_short_pressure_and_recovery_do_not_notify(self):
        healthy=sample();busy=copy.deepcopy(healthy)
        busy['host']['io_psi']['full']['avg10']=25.5
        samples=[]
        for i,base in enumerate([busy]*18+[healthy]*15):
            row=copy.deepcopy(base);row['time']=100+i*5;samples.append(row)
        stop=Mock();stop.is_set.side_effect=[False]*len(samples)+[True]
        with patch.object(d.metrics,'shared_snapshot',side_effect=samples),patch.object(self.delivery,'deliver'),patch.object(d,'DiskCapacity'):
            d.watch(self.delivery,stop)
        self.assertEqual(self.delivery.data['pending'],[])

    @patch.object(d,'request')
    def test_weekly_delivered_once_even_after_restart(self,request):
        request.side_effect=[{'id':'channel'},{'id':'message'}]
        event={'kind':'weekly','scope':'2026-W45','message':'brief'}
        self.assertTrue(self.delivery.enqueue(event));self.assertTrue(self.delivery.deliver())
        self.delivery.lock.close()
        again=d.Delivery(self.credentials,self.delivery.path.parent)
        self.addCleanup(again.lock.close)
        self.assertFalse(again.enqueue(event))

    def test_resolved_pending_pressure_is_discarded(self):
        self.delivery.enqueue({'kind':'pressure','message':'pressure'},incident='episode')
        self.delivery.cancel_incident('episode')
        self.assertEqual(self.delivery.data['pending'],[])
        self.assertFalse(self.delivery.enqueue({'kind':'recovered','message':'recovered'}))

    @patch.object(d,'request')
    def test_separate_oom_incidents_not_suppressed_by_cooldown(self,request):
        request.side_effect=[{'id':'channel'},{'id':'1'},{'id':'2'}]
        for number in [1,2]:
            self.assertTrue(self.delivery.enqueue({'kind':'oom','message':'OOM'},incident=str(number)))
            self.assertTrue(self.delivery.deliver())

    @patch.object(d,'request')
    def test_outdated_pressure_not_replayed_after_outage(self,request):
        self.delivery.enqueue({'kind':'pressure','message':'old'},incident='old')
        self.delivery.data['pending'][0]['created']-=901
        self.assertFalse(self.delivery.deliver());request.assert_not_called()
