import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from test_resource_guard import sample
import alert_policy as a
import weekly_brief as w

class PolicyTests(unittest.TestCase):
    def delivery(self):
        delivery=Mock();delivery.data={};delivery.mutex=Mock();delivery.mutex.__enter__=Mock();delivery.mutex.__exit__=Mock()
        return delivery

    @patch.object(a,'resource_context',return_value='- guest / compiler PID 12: RSS 2GiB')
    def test_ten_minutes_attributed_pressure_once_and_no_recovery_spam(self,context):
        delivery=self.delivery();policy=a.Incidents(delivery)
        for i in range(121):
            row=sample();row['time']=100+i*5;row['host']['io_psi']['full']['avg10']=25
            policy.update(row)
        self.assertEqual(delivery.enqueue.call_count,1)
        message=delivery.enqueue.call_args.args[0]['message']
        self.assertIn('10分'.replace('分','분'),message);self.assertIn('compiler',message)
        for i in range(15):
            row=sample();row['time']=705+i*5;policy.update(row)
        self.assertEqual(delivery.enqueue.call_count,1);delivery.cancel_incident.assert_called_once()

    @patch.object(a,'resource_context',return_value='')
    def test_no_blame_without_attributable_tasks(self,context):
        delivery=self.delivery();policy=a.Incidents(delivery)
        for i in range(130):
            row=sample();row['time']=100+i*5;row['host']['io_psi']['full']['avg10']=25;policy.update(row)
        delivery.enqueue.assert_not_called()

    @patch.object(a,'resource_context',return_value='process')
    def test_monitoring_gap_not_counted_as_continuous_pressure(self,context):
        delivery=self.delivery();policy=a.Incidents(delivery)
        for t in [100,105,1000]:
            row=sample();row['time']=t;row['host']['io_psi']['full']['avg10']=25;policy.update(row)
        delivery.enqueue.assert_not_called()

    def test_weekly_handles_guest_replacement_without_counter_double_count(self):
        def journal(*args,**kwargs):
            if '-k' in args:return iter([])
            if 'coolify-fixkit-discord' in args:return iter([])
            rows=[]
            for t,ident,write in [(100,1,100),(160,1,200),(220,2,9000),(280,2,9100)]:
                rows.append(json.dumps({'event':'host-io','time':t,'guests':{'lxc.payload.test':{'identity':ident,'current':1024**3,'max':str(2*1024**3),'io':{'wbytes':write}}},'top_io':[]}))
            return iter(rows)
        with patch.object(w,'journal',side_effect=journal):
            body,week=w.build_brief()
        self.assertIn('4개 표본',body);self.assertIn('test: 1.00GiB',body);self.assertLessEqual(len(body),1200)
