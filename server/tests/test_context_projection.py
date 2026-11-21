"""The executor must consume the single validated plan without replanning."""
import unittest
from pydantic import ValidationError
from app.services.turn_planner import plan_from_llm_payload, validate_turn_plan, PlanResult
from app.routers.agent import _context_plan_from_turn_plan, _draft_seed_from_plan
from app.memory.planning import memory_read_plan_from_turn_plan


class ContextProjectionTest(unittest.TestCase):
    def plan(self,intent,parameters=None,sources=None,page=None,mode='chat'):
        return validate_turn_plan(plan_from_llm_payload({'primary_intent':intent,'intent_parameters':parameters or {},
            'context_sources':sources or [],'confidence':0.95,'reason':'synthetic planner contract'},mode=mode),page_state=page)

    def test_draft_edit_search_and_chat_executor_contracts(self):
        draft=self.plan('note_create',{'topic':'Redis','requirements':'面向初学者'})
        context=_context_plan_from_turn_plan(draft)
        self.assertEqual(context.primary_intent,'note_draft_create');self.assertEqual(context.reply_surface,'draft_workspace')
        self.assertEqual(_draft_seed_from_plan(context,'raw request'),'Redis')
        edit=self.plan('note_edit',{'instruction':'简化表达'},['current_note'],{'currentNoteId':'note-1'})
        self.assertEqual(edit.intent_parameters.note_id,'note-1');self.assertEqual(edit.result,PlanResult.EXECUTE)
        context=_context_plan_from_turn_plan(edit);self.assertEqual(context.reply_surface,'editor_patch')
        self.assertTrue(context.context_plan['use_current_note']);self.assertEqual(context.edit_request['instruction'],'简化表达')
        for mode,search in [('chat',False),('ask_notes',True)]:
            context=_context_plan_from_turn_plan(self.plan('general_chat',mode=mode,page={'currentNoteId':'note-1'}))
            self.assertEqual(context.context_plan['search_note_library'],search)
            self.assertFalse(context.context_plan['use_current_note'])

    def test_selection_and_missing_fields_remain_explicit(self):
        plan=self.plan('note_edit',{'instruction':'更简洁'},['selected_text'],{'currentNoteId':'note-1','selectedText':'原文'})
        self.assertTrue(_context_plan_from_turn_plan(plan).context_plan['use_current_selection'])
        self.assertEqual(self.plan('note_edit').result,PlanResult.CLARIFY)
        self.assertEqual(self.plan('note_create').result,PlanResult.CLARIFY)

    def test_memory_actions_remain_typed_without_semantic_fallback(self):
        for action in ['read','list','write','update','delete','disable']:
            plan=self.plan('memory',{'memory_action':action,'memory_key':'profile.age','memory_scope':'global',
                'memory_layers':['semantic'],'memory_types':['personal_info']},['user_memory'])
            context=_context_plan_from_turn_plan(plan)
            self.assertEqual(context.primary_intent,'memory_manage');self.assertEqual(context.memory_action,action)
            read=memory_read_plan_from_turn_plan(plan,question='合成记忆请求')
            self.assertEqual(read.canonical_keys,['profile.age']);self.assertEqual(read.memory_types,['personal_info'])
        with self.assertRaises(ValidationError):self.plan('memory',{'memory_action':'dump_all_users'})
