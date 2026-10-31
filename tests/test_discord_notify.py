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

    def test_pressure_transition_records_metrics_then_recovers_without_spam(self):
        healthy=sample();busy=copy.deepcopy(healthy)
        busy['host']['io_psi']['full']['avg10']=25.5
        stop=Mock();stop.is_set.side_effect=[False]*6+[True]
        output=io.StringIO()
        with patch.object(d.metrics,'snapshot',side_effect=[busy]*3+[healthy]*3),patch.object(self.delivery,'deliver'),patch('sys.stdout',output):
            d.watch(self.delivery,stop)
        self.assertEqual([x['key'] for x in self.delivery.data['pending']],['pressure:debian','recovered:debian'])
        self.assertIn('12288MiB',self.delivery.data['pending'][0]['text'])
        self.assertIn('25.50%',self.delivery.data['pending'][0]['text'])
        events=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([x['event'] for x in events],['resource-pressure','resource-recovered'])
        self.assertEqual(events[0]['io_full_avg10'],25.5)
        self.assertFalse(self.delivery.data['pressure_active'])
