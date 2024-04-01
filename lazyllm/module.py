from .flow import FlowBase, Pipeline, Parallel, DPES
import os
import lazyllm
from lazyllm import FlatList
from collections import Iterable
import httpx
from lazyllm.thirdparty import gradio as gr
from pydantic import BaseModel as struct
from typing import Tuple
from types import GeneratorType
import multiprocessing


class ModuleBase(object):
    def __init__(self):
        self.submodules = []
        self._evalset = None

    def __setattr__(self, name: str, value):
        if isinstance(value, ModuleBase):
            self.submodules.append(value)
        return super().__setattr__(name, value)

    def __call__(self, *args, **kw): return self.forward(*args, **kw)

    # interfaces
    def forward(self, *args, **kw): raise NotImplementedError
    def _get_train_tasks(self): return None
    def _get_deploy_tasks(self): return None

    def evalset(self, evalset, load_f=None, collect_f=lambda x:x):
        if isinstance(evalset, str) and os.path.exists(evalset):
            with open(evalset) as f:
                assert callable(load_f)
                self._evalset = load_f(f)
        else:
            self._evalset = evalset
        self.eval_result_collet_f = collect_f

    # TODO: add lazyllm.eval
    def _get_eval_tasks(self):
        def set_result(x): self.eval_result = x
        if self._evalset:
            return Pipeline(lambda: [self(**item) if isinstance(item, dict) else self(item)
                                     for item in self._evalset],
                            lambda x: self.eval_result_collet_f(x),
                            set_result)
        return None

    # update module(train or finetune), 
    def update(self, *, mode='train', recursive=True):
        assert mode in ('train', 'server')
        # dfs to get all train tasks
        train_tasks, deploy_tasks, eval_tasks = FlatList(), FlatList(), FlatList()
        stack = [(self, iter(self.submodules if recursive else []))]
        while len(stack) > 0:
            try:
                top = next(stack[-1][1])
                stack.append((top, iter(top.submodules)))
            except StopIteration:
                top = stack.pop()[0]
                train_tasks.absorb(top._get_train_tasks())
                deploy_tasks.absorb(top._get_deploy_tasks())
                eval_tasks.absorb(top._get_eval_tasks())

        if mode == 'train' and len(train_tasks) > 0:
            Parallel(*train_tasks).start().wait()
        if len(deploy_tasks) > 0:
            DPES(*deploy_tasks).start()
        if mode == 'train' and len(eval_tasks) > 0:
            DPES(*eval_tasks).start()

    def update_server(self, *, recursive=True): return self.update(mode='server', recursive=recursive)
    def start(self): return self.update(mode='server', recursive=True)
    def restart(self): return self.start()

    def _overwrote(self, f):
        return getattr(self.__class__, f) is not getattr(__class__, f)


class ModuleResponse(struct):
    messages: str = ''
    trace: str = ''
    err: Tuple[int, str] = (0, '')


class SequenceModule(ModuleBase):
    def __init__(self, *args):
        super().__init__()
        self.submodules = list(args)

    def forward(self, *args, **kw):
        ppl = Pipeline(*self.submodules)
        return ppl.start(*args, **kw)

    def __repr__(self):
        representation = '<SequenceModule> [\n'
        for m in self.submodules:
            representation += '\n'.join(['    ' + s for s in repr(m).split('\n')]) + '\n'
        return representation + ']'
    

class UrlModule(ModuleBase):
    def __init__(self, url, *, remote_prompt=False):
        super().__init__()
        self._url, self._prompt_url = url, None
        self._remote_prompt = remote_prompt
        self._prompt, self._response_split = '{input}', None

    def url(self, url):
        print('url:', url)
        self._url = url
        if self._remote_prompt:
            self._prompt_url = url[::-1].replace('/generate'[::-1], '/prompt'[::-1], 1)[::-1]
        
    def forward(self, __input=None, **kw):
        assert self._url is not None, f'Please start {self.__class__} first'
        assert (__input is None) ^ (len(kw) == 0)

        if __input is not None:
            kw['input'] = __input
        if not self._remote_prompt:
            kw = dict(input=self._prompt.format(**kw))

        with httpx.Client(timeout=90) as client:
            response = client.post(self._url, json=kw,
                                   headers={'Content-Type': 'application/json'})
        return response.text if self._remote_prompt or self._response_split is None else \
               response.text.split(self._response_split)[-1]

    def prompt(self, prompt='{input}', response_split=None, update_remote=True):
        if self._remote_prompt and self._prompt_url and update_remote and (
                self._prompt != prompt or self._response_split != response_split):
            with httpx.Client(timeout=90) as client:
                r = client.post(self._prompt_url, json=dict(prompt=prompt, response_split=response_split),
                                headers={'Content-Type': 'application/json'}).text
                assert r == 'set prompt done!'
            self._prompt, self._response_split = prompt, response_split
        return self


class ActionModule(ModuleBase):
    def __init__(self, action):
        super().__init__()
        if isinstance(action, FlowBase):
            action.for_each(lambda x: isinstance(x, ModuleBase), lambda x: self.submodules.append(x))
        self.action = action

    def forward(self, *args, **kw):
        if isinstance(self.action, FlowBase):
            r = self.action.start(*args, **kw).result
        else:
            r = self.action(*args, **kw)
        return r

    def __repr__(self):
        representation = '<ActionModule> ['
        if isinstance(self.action, (FlowBase, ActionModule, ServerModule, SequenceModule)):
            sub_rep = '\n'.join(['    ' + s for s in repr(self.action).split('\n')])
            representation += '\n' + sub_rep + '\n'
        else:
            representation += repr(self.action)
        return representation + ']'


class ServerModule(UrlModule):
    def __init__(self, m, pre=None, post=None):
        super().__init__(url=None, remote_prompt=True)
        self.m = m
        self._pre_func = pre
        self._post_func = post

    def _get_deploy_tasks(self):
        return Pipeline(
            lazyllm.deploy.RelayServer(func=self.m, pre_func=self._pre_func, post_func=self._post_func),
            self.url)
    
    # change to urlmodule when pickling to server process
    def __reduce__(self):
        assert hasattr(self, '_url') and self._url is not None
        m = UrlModule(self._url, remote_prompt=self._remote_prompt).prompt(
                prompt=self._prompt, response_split=self._response_split, update_remote=False)
        return m.__reduce__()

    def __repr__(self):
        representation = '<ServerModule> ['
        if isinstance(self.action, (FlowBase, ActionModule, ServerModule, SequenceModule)):
            sub_rep = '\n'.join(['    ' + s for s in repr(self.action).split('\n')])
            representation += '\n' + sub_rep + '\n'
        else:
            representation += repr(self.action)
        return representation + ']'


css = """
#logging {background-color: #FFCCCB}
"""
class WebModule(ModuleBase):
    def __init__(self, m, *, title='对话演示终端', stream_output=True) -> None:
        super().__init__()
        self.m = m
        self.title = title
        self.demo = self.init_web()

    def init_web(self):
        with gr.Blocks(css=css, title=self.title) as demo:
            with gr.Row():
                with gr.Column(scale=3):
                    chat_use_context = gr.Checkbox(interactive=True, value=False, label="使用上下文")
                    stream_output = gr.Checkbox(interactive=True, value=True, label="流式输出")
                    dbg_msg = gr.Textbox(show_label=True, label='处理日志', elem_id='logging', interactive=False, max_lines=10)
                    clear_btn = gr.Button(value="🗑️  Clear history", interactive=True)
                with gr.Column(scale=6):
                    chatbot = gr.Chatbot(height=600)
                    query_box = gr.Textbox(show_label=False, placeholder='输入内容并回车!!!')

            query_box.submit(self._prepare, [query_box, chatbot, stream_output], [query_box, chatbot], queue=False
                ).then(self._respond_stream, [chat_use_context, chatbot], [chatbot, dbg_msg], queue=chatbot
                ).then(lambda: gr.update(interactive=True), None, query_box, queue=False)
            clear_btn.click(self._clear_history, None, outputs=[chatbot, query_box, dbg_msg])
        return demo

    def _prepare(self, query, chat_history, stream_output):
        if chat_history is None:
            chat_history = []
        self.m.stream_output = stream_output
        return '', chat_history + [[query, None]]
        
    def _respond_stream(self, use_context, chat_history):
        try:
            # TODO: move context to trainable module
            input = ('\<eos\>'.join([f'{h[0]}\<eou\>{h[1]}' for h in chat_history]).rsplit('\<eou\>', 1)[0]
                     if use_context else chat_history[-1][0])
            result, log = self.m(input), None
            def get_log_and_message(s, log=''):
                return ((s.messages, s.err[1] if s.err[0] != 0 else s.trace) 
                        if isinstance(s, ModuleResponse) else (s, log))
            if isinstance(result, (ModuleResponse, str)):
                chat_history[-1][1], log = get_log_and_message(result)
            elif isinstance(result, GeneratorType):
                chat_history[-1][1] = ''
                for s in result:
                    if isinstance(s, (ModuleResponse, str)):
                        s, log = get_log_and_message(s, log)
                    chat_history[-1][1] += s
                    yield chat_history, log
            else:
                raise TypeError('function result should only be ModuleResponse or str')
        except Exception as e:
            chat_history = None
            log = str(e)
        yield chat_history, log

    def _clear_history(self):
        return [], '', ''

    def _work(self):
        def _impl():
            self.demo.queue().launch(server_name="0.0.0.0", server_port=20566)
        self.p = multiprocessing.Process(target=_impl)
        self.p.start()

    def _get_deploy_tasks(self):
        return Pipeline(self._work)

    def wait(self):
        return self.p.join()


class TrainableModule(UrlModule):
    def __init__(self, base_model, target_path):
        super().__init__(url=None, remote_prompt=True)
        self.base_model = base_model
        self.target_path = target_path
        self._train = None # lazyllm.train.auto
        self._finetune = lazyllm.finetune.auto
        self._deploy = None # lazyllm.deploy.auto
    
    def _get_train_tasks(self):
        trainset_getf = lambda : lazyllm.package(self._trainset, None) \
                        if isinstance(self._trainset, str) else self._trainset
        if self._mode == 'train':
            train = self._train(self.base_model, os.path.join(self.target_path, 'train'))
        elif self._mode == 'finetune':
            train = self._finetune(self.base_model, os.path.join(self.target_path, 'finetune'))
        else:
            raise RuntimeError('mode must be train or finetune')
        return Pipeline(trainset_getf, train)

    def _get_deploy_tasks(self):
        return Pipeline(lambda *a: self.target_path,
            self._deploy(pre_func=self._pre_func, post_func=self._post_func),
            self.url)

    def __getattr__(self, key):
        def _setattr(v):
            setattr(self, f'_{key}', v)
            return self
        keys = ['trainset', 'train', 'finetune', 'deploy', 'pre_func', 'post_func', 'mode']
        if key in keys:
            return _setattr
        elif key.startswith('_') and key[1:] in keys:
            return None
        raise AttributeError(f'{__class__} object has no attribute {key}')

    # change to urlmodule when pickling to server process
    def __reduce__(self):
        assert hasattr(self, '_url') and self._url is not None
        m = UrlModule(self._url, remote_prompt=self._remote_prompt).prompt(
                prompt=self._prompt, response_split=self._response_split, update_remote=False)
        return m.__reduce__()

    def __repr__(self):
        mode = '-Train' if self._mode == 'train' else (
               '-Finetune' if self._mode == 'finetune' else '')
        return f'<TrainableModule{mode}> [{self.base_model}]'


class Module(object):
    # modules(list of modules) -> SequenceModule
    # action(lazyllm.flow) -> ActionModule
    # url(str) -> UrlModule
    # base_model(str) & target_path(str)-> TrainableModule
    def __new__(self, *args, **kw):
        if len(args) >= 1 and isinstance(args[0], Module):
            return SequenceModule(*args)
        elif len(args) == 1 and isinstance(args[0], list) and isinstance(args[0][0], Module):
            return SequenceModule(*args[0])
        elif len(args) == 0 and 'modules' in kw:
            return SequenceModule(kw['modules'])
        elif len(args) == 1 and isinstance(args[0], FlowBase):
            return ActionModule(args[0])
        elif len(args) == 0 and 'action' in kw:
            return ActionModule(kw['modules'])
        elif len(args) == 1 and isinstance(args[0], str):
            return UrlModule(args[0])
        elif len(args) == 0 and 'url' in kw:
            return UrlModule(kw['url'])
        elif ...:
            return TrainableModule()

    @classmethod
    def sequence(cls, *args, **kw): return SequenceModule(*args, **kw)
    @classmethod
    def action(cls, *args, **kw): return ActionModule(*args, **kw)
    @classmethod
    def url(cls, *args, **kw): return UrlModule(*args, **kw)
    @classmethod
    def trainable(cls, *args, **kw): return TrainableModule(*args, **kw)


# TODO(wangzhihong): remove these examples
# Examples:

m1 = Module.url('1')
m2 = Module.url('2')

seq_m = Module.sequence(m1, m2)
act_m = Module.action(Pipeline(seq_m, m2))

class MyModule(ModuleBase):
    def __init__(self):
        super().__init__()
        self.m1 = act_m
        self.m2 = seq_m 

    def forward(self, *args, **kw):
        ppl = Pipeline(self.m1, self.m2)
        ppl.start()

my_m = MyModule()