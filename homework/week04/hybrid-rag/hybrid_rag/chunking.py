"""Shared structure-aware chunks for vector and keyword retrieval.

Markdown headings travel with substantive text; Python uses top-level AST units.
The tokenizer budget is shared across both embedding models.
"""
from __future__ import annotations
import ast
import csv
import hashlib
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from llama_index.core import Document
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import TextNode
from tokenizers import Tokenizer

@lru_cache(maxsize=1)
def tools():
    path = Path(__file__).resolve().parents[1] / '.model-cache/bge-sentence-transformers/tokenizer.json'
    if not path.is_file():
        raise ValueError('shared BGE tokenizer missing; run download_bge.py first')
    tokenizer = Tokenizer.from_file(str(path))
    tokenizer.no_truncation()
    splitter = SentenceSplitter(chunk_size=384, chunk_overlap=0,
        tokenizer=lambda text: tokenizer.encode(text, add_special_tokens=False).ids)
    return tokenizer, splitter

def load_nodes(root: Path):
    tok, splitter = tools()
    parser = MarkdownNodeParser()
    parts=[]
    for path in sorted((root/'corpus/current').rglob('*')):
        if not path.is_file():continue
        rel=path.relative_to(root/'corpus/current').as_posix()
        text=path.read_text()
        if path.suffix=='.md':
            body=re.sub(r'\A---\n.*?\n---\n','',text,flags=re.S)
            sections=parser.get_nodes_from_documents([Document(text=body)])
            intro=''
            if len(sections)>1:
                first_lines=sections[0].text.strip().splitlines()
                intro='\n'.join(first_lines[1:]).strip() if first_lines and first_lines[0].startswith('# ') else ''
            for section in sections:
                lines=section.text.strip().splitlines()
                if not lines:continue
                first=lines[0]
                own=re.sub(r'^#+\s*','',first) if first.startswith('#') else ''
                parent=section.metadata.get('header_path','').strip('/').replace('/',' > ')
                heading=' > '.join(filter(None,[parent,own]))
                content='\n'.join(lines[1:]).strip() if own else section.text.strip()
                if not content:continue
                if first.startswith('# ') and len(sections)>1:continue
                payload=f'# {heading}\n\n'+(intro+'\n\n' if intro and parent else '')+content
                for chunk in splitter.split_text(payload):parts.append((rel,heading,chunk))
        elif path.suffix=='.py':
            tree=ast.parse(text)
            lines=text.splitlines()
            imports='\n'.join(ast.get_source_segment(text,x) or '' for x in tree.body if isinstance(x,(ast.Import,ast.ImportFrom)))
            spans=[]
            for item in tree.body:
                if isinstance(item,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                    start=min([item.lineno]+[d.lineno for d in item.decorator_list])-1
                    spans.append((start,item.end_lineno,item.name))
                elif isinstance(item,(ast.Assign,ast.AnnAssign)):
                    targets=item.targets if isinstance(item,ast.Assign) else [item.target]
                    heading=', '.join(ast.unparse(t) for t in targets)
                    spans.append((item.lineno-1,item.end_lineno,heading))
            if not spans:spans=[(0,len(lines),'(模块)')]
            for start,end,name in spans:
                payload=f'# {rel} > {name}\n\n'+(imports+'\n\n' if imports else '')+'\n'.join(lines[start:end])
                for chunk in splitter.split_text(payload):parts.append((rel,name,chunk))
        elif path.suffix=='.csv':
            for row in csv.DictReader(text.splitlines()):
                parts.append((rel,next(iter(row.values())),'；'.join(f'{k}: {v}' for k,v in row.items())))
        else:
            raise ValueError(f'unsupported current source: {rel}')
    nodes=[]
    per_file=Counter()
    for rel,heading,text in parts:
        i=per_file[rel];per_file[rel]+=1
        node_id=hashlib.sha256(f'{rel}\0{heading}\0{i}\0{text}'.encode()).hexdigest()
        metadata={'file_path':rel,'heading':heading,'part':i,'status':'current'}
        nodes.append(TextNode(text=text,id_=node_id,metadata=metadata,excluded_embed_metadata_keys=list(metadata),excluded_llm_metadata_keys=list(metadata)))
    signature=hashlib.sha256(json.dumps({'mode':'md_ast','nodes':[n.node_id for n in nodes]},sort_keys=True).encode()).hexdigest()
    return nodes,signature
