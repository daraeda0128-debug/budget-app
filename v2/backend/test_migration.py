import unittest
from migrate import plan

class MigrationTests(unittest.TestCase):
    def fixture(self):
        return {'tx':{'2026-09':{'a':{'date':'2026-09-01','name':'test','cat':'식비','amt':1200,'amount':1200,'type':'expense','who':'j','payMethod':'card'}}},'config':{'salaryJ':100,'pin_master':'1234'}}
    def test_preserves_key_and_totals(self):
        p = plan(self.fixture(),{})
        self.assertEqual(p['transactions'][0]['id'],'firebase/tx/2026-09/a')
        self.assertEqual(p['report']['totals'],{'2026-09/j/expense/card/식비':1200})
        self.assertNotIn('pin_master',p['config'])
    def test_conflict_blocks_review(self):
        self.assertEqual(plan(self.fixture(),{'salaryJ':'200'})['report']['conflicts'],['config/salaryJ'])
    def test_inconsistent_amount_rejected(self):
        f = self.fixture(); f['tx']['2026-09']['a']['amount']=3
        with self.assertRaises(ValueError): plan(f,{})
    def test_missing_payment_remains_unknown(self):
        f = self.fixture(); del f['tx']['2026-09']['a']['payMethod']
        self.assertEqual(plan(f,{})['report']['missing_payment'],1)
    def test_month_mismatch_rejected(self):
        f = self.fixture(); f['tx']['2026-09']['a']['date']='2026-10-01'
        with self.assertRaises(ValueError): plan(f,{})

if __name__=='__main__': unittest.main()
