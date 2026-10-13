"""
yaml
k: v
"""
import yaml
from utils.path_tool import get_abs_path


def load_rag_config(config_path: str=get_abs_path("config/rag.yaml"),encoding: str='utf-8'):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)   # 将配置文件全量加载

def load_chroma_config(config_path: str=get_abs_path("config/chroma.yaml"),encoding: str='utf-8'):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)   # 将配置文件全量加载

def load_prompt_config(config_path: str=get_abs_path("config/prompt.yaml"),encoding: str='utf-8'):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)   # 将配置文件全量加载


def load_agent_config(config_path: str=get_abs_path("config/agent.yaml"),encoding: str='utf-8'):
    with open(config_path,"r",encoding=encoding) as f:
        return yaml.load(f,Loader=yaml.FullLoader)   # 将配置文件全量加载


rag_conf = load_rag_config()
chroma_conf = load_chroma_config()
prompt_conf = load_prompt_config()
agent_conf = load_agent_config()


if __name__ == '__main__':
    print(rag_conf["chat_model_name"])