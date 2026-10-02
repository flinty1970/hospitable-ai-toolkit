import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from hosting.account_details import enrich_channels

class ChannelMetadataTests(unittest.TestCase):
    def test_markups_and_exact_account_match(self):
        channels=Mock(status_code=200)
        channels.json.return_value={'data':[{'platform':'airbnb','user_id':'123'}]}
        detail=Mock(status_code=200)
        detail.json.return_value={'data':{'listings':[{'platform':'airbnb','platform_user_id':'123'},{'platform':'agoda','platform_user_id':'999'},{'platform':'direct'}], 'bookings':{'listing_markups':[{'platform':'airbnb','type':'percent','markup':0},{'platform':'agoda','type':'percent','markup':60},{'platform':'direct','type':'percent','markup':-5}]}}}
        properties={'p':{}}
        with patch.dict(os.environ,{'HOSPITABLE_PAT':'secret'}),patch('hosting.account_details.requests.get',side_effect=[channels,detail]) as get:
            enrich_channels('.',{'api_key_env':'HOSPITABLE_PAT','properties':{'p':{}}},properties)
        rows=properties['p']['channels']
        self.assertEqual([r['markup'] for r in rows],['0% (API)','60% (API)','-5% (API)'])
        self.assertEqual(rows[0]['connection_status'],'Connected account matched')
        self.assertEqual(rows[1]['connection_status'],'Not confirmed by API')
        self.assertEqual(get.call_args.kwargs['params'],{'include':'listings,bookings'})
        self.assertNotIn('secret',str(properties))

    def test_denied_channels_and_missing_markup_remain_unknown(self):
        channels=Mock(status_code=403)
        detail=Mock(status_code=200)
        detail.json.return_value={'data':{'listings':[{'platform':'vrbo','platform_user_id':'123'}]}}
        properties={'p':{}}
        with patch.dict(os.environ,{'HOSPITABLE_PAT':'secret'}),patch('hosting.account_details.requests.get',side_effect=[channels,detail]):
            enrich_channels('.',{'api_key_env':'HOSPITABLE_PAT','properties':{'p':{}}},properties)
        row=properties['p']['channels'][0]
        self.assertEqual(row['markup'],'Markup not supplied by API')
        self.assertEqual(row['connection_status'],'Not confirmed by API')

    def test_hide_sources_and_prefer_homeaway_evidence(self):
        from hosting.account_details import display_channels
        rows=[{'channel':p,'markup':m,'connection_status':c} for p,m,c in [('gvr','missing','unknown'),('manual','missing','unknown'),('vrbo','missing','unknown'),('homeaway','15% (API)','Connected account matched')]]
        self.assertEqual(display_channels(rows),[{'channel':'Vrbo','markup':'15% (API)','connection_status':'Connected account matched'}])
