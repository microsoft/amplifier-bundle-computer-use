"""Observed selection keeps canonical approval, freshness, and read-only gates."""
import io
import time
from types import SimpleNamespace
import pytest
from PIL import Image

from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.observed_control import ObservedControl
from test_computer_tool_coexistence import _FakeBackend


class Backend(_FakeBackend):
    def __init__(self): super().__init__();self.color='white'
    def capture(self,region=None):
        buf=io.BytesIO();Image.new('RGB',(800,600),self.color).save(buf,format='PNG');return buf.getvalue()


def setup(config=None):
    backend=Backend();tool=ComputerTool(backend,{'gate_writes':True,**(config or {})});tool.resolve_display()
    tool._ensure_announced()
    tool._observed_control=ObservedControl(tool)
    # Supply observed candidates independently of OCR recognition quality.
    raw,digest,w,h=tool._observed_control.capture(tool._binding)
    observed={'revision':'proof','state':{'elements':[{'id':'button','rect':[10,20,100,30]}]},
        'candidates':[{'id':'click','operation':'CLICK','target':'button'}],'created_at':time.monotonic()}
    tool._observed_control.observations['proof']={'observation':observed,'binding':tool._binding,'digest':digest}
    return tool,backend


async def act(tool): return await tool.execute({'action':'act','revision':'proof','candidate':'click'})


@pytest.mark.asyncio
async def test_exact_canonical_action_is_approved_before_click():
    tool,backend=setup();seen=[]
    async def approve(prepared,binding):seen.append(prepared);return True
    tool._write_approval=approve
    result=await act(tool)
    assert result.success and result.output['status']=='completed'
    assert seen==[{'action':'left_click','coordinate':[60,35]}]
    assert backend.calls==[('click',60,35,'left',1)]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['denied','read_only','missing_approval'])
async def test_existing_gate_prevents_observed_write(mode):
    tool,backend=setup({'read_only':mode=='read_only'})
    if mode!='missing_approval':
        async def approve(prepared,binding):return mode!='denied'
        tool._write_approval=approve
    result=await act(tool)
    assert result.output['status']=='denied' and not backend.calls


@pytest.mark.asyncio
async def test_screen_change_during_approval_stops_before_input():
    tool,backend=setup()
    async def approve(prepared,binding):backend.color='black';return True
    tool._write_approval=approve
    result=await act(tool)
    assert result.output['status']=='stale' and not backend.calls


@pytest.mark.asyncio
async def test_presence_wait_precedes_final_observation_check():
    tool,backend=setup()
    class Guard:
        def check_start_permission(self):pass
        def bind_target(self):pass
        def before_event(self,**kw):backend.color='black'
        def after_event(self):pass
        def release_target(self):pass
    tool._coexistence_guard=Guard()
    # Policy installation replaces the binding; re-observe that intended target.
    tool._observed_control.observations['proof']['binding']=tool._binding
    async def approve(prepared,binding):return True
    tool._write_approval=approve
    result=await act(tool)
    assert result.output['status']=='stale' and not backend.calls


@pytest.mark.asyncio
async def test_expired_observation_unobserved_candidate_and_remote_target():
    tool,backend=setup()
    tool._observed_control.observations['proof']['observation']['created_at']-=20
    assert (await act(tool)).output['status']=='stale'
    assert (await tool.execute({'action':'act','revision':'invented','candidate':'click'})).output['status']=='stale'
    assert not backend.calls


def test_extension_is_absent_by_default():
    tool=ComputerTool(Backend(),{})
    assert 'observe' not in tool.input_schema['properties']['action']['enum']
    assert tool._observed_control is None


@pytest.mark.asyncio
async def test_approval_reading_time_keeps_exact_unchanged_target():
    tool,backend=setup()
    async def approve(prepared,binding):
        tool._observed_control.observations['proof']['observation']['created_at']-=20
        return True
    tool._write_approval=approve
    assert (await act(tool)).output['status']=='completed'
    assert backend.calls==[('click',60,35,'left',1)]
