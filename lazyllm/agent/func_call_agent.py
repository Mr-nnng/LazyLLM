from typing import Dict, Iterator, List, Any, Optional, Union
import copy

from lazyllm.module import OnlineChatModule
from lazyllm.module import TrainableModule

from lazyllm.agent.protocol import DEFAULT_SYSTEM_MESSAGE, Message, ROLE, CONTENT, NAME, TOOL_CALLS, ASSISTANT, TOOL
from lazyllm.agent.base import BaseAgent
from lazyllm.agent.configs import MAX_CONSECUTIVE_TOOL_CALL_NUM
from lazyllm.agent.tools import BaseTool


class FuncCallAgent(BaseAgent):
    def __init__(self, 
                 llm:Union[OnlineChatModule, TrainableModule],
                 tools_list: Optional[List[Union[str, Dict, BaseTool]]] = None,
                 system_message:str = DEFAULT_SYSTEM_MESSAGE,
                 name: Optional[str] = None,
                 description: Optional[str] = None,
                 return_trace: bool = False,
                 **kwargs) -> None:
        """
        Initialize the agent.

        Args:
            llm (Union[OnlineChatModule, TrainableModule]): The LLM to use for the agent.
            tools_list (Optional[List[Union[str, Dict, BaseTool]]]): A list of tools the agent can use.
            system_message (str): The system message to use for the agent.
            name (Optional[str]): The name of the agent.
            description (Optional[str]): The description of the agent.
            return_trace (bool): Whether to return the trace of the agent's actions.
            **kwargs: Additional keyword arguments to pass to the agent.
        """
        super().__init__(llm, tools_list, system_message, name, description, return_trace, **kwargs)

    
    def _run(self,
             __input:Message, 
             llm_chat_history:List[Message],
             tools:List[str] = None,
             generate_cfg: Dict[str, Any] = None,
             **kwargs) -> Iterator[Message]:
        llm_chat_history = copy.deepcopy(llm_chat_history)
        last_role, content_cache = None, []

        tool_call_counter = 0
        while tool_call_counter < MAX_CONSECUTIVE_TOOL_CALL_NUM:
            llm_response = self._call_llm(__input, llm_chat_history, tools, generate_cfg=generate_cfg, **kwargs)
            for response in llm_response:
                yield response
                role = response.get(ROLE, None)
                if role: # role不为空，是新消息
                    if content_cache:
                        llm_chat_history.append(Message(role=last_role, content="".join(content_cache)))
                        content_cache = []
                    last_role = role
                content = response.get(CONTENT, None)
                if content:
                    content_cache.append(content)
                tool_calls = response.get(TOOL_CALLS, None)
                if tool_calls:
                    assert last_role == ASSISTANT, f"Tool calls can only be made by the {ASSISTANT}."
                    llm_chat_history.append(Message(role=ASSISTANT, content="".join(content_cache), tool_calls=tool_calls))
                    content_cache = []
                    for tool_call in tool_calls:
                        tool_id = tool_call["id"]
                        tool_name = tool_call["function"]["name"]
                        tool_args = tool_call["function"]["arguments"]
                        tool_res = self._call_tool(tool_name, tool_args, **kwargs)
                        if tool_res is not None:
                            tool_message = Message(role=TOOL, content=tool_res, name=tool_name, tool_call_id=tool_id)
                            llm_chat_history.append(tool_message)
                            yield tool_message
                    tool_call_counter += 1
                
            if content_cache:
                llm_chat_history.append(Message(role=last_role, content="".join(content_cache)))
                content_cache = []
            if llm_chat_history[-1][ROLE] != TOOL:
                break
        print("history:", llm_chat_history)