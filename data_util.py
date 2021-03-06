import re
import json
import torch
import nltk
import random
import seg_passage

from nltk.corpus import stopwords

from collections import Counter


max_tokens = 20  # tokens number in query
max_sents = 10   # sents number in doc
m = 250  # window size
n = 125   # step size
n_p = 3  # negtive/positive
d_emb = 300

pad_id = 0  # fix
unk_id = 1  # fix


def load_docs(file_path):
    docs = list() 
    with open(file_path, 'r', encoding='utf-8') as f:
        for idx, line in enumerate(f):
            if idx == 0:
                continue
            # qid, query, doc_id, doc, label = line.strip().split("\t")  # event格式
            # docs.append((int(qid), query, int(doc_id), doc, int(label)))  # event
            # qid, query, _, _, doc_id, doc, label = line.strip().split("\t")  # QA格式
            # docs.append((int(qid[-1:]), query, int(doc_id[-1:]), doc, int(label)))  # QA格式
            # qid, query, doc_id, doc, label = line.strip().split("\t")  # robust格式
            # docs.append((int(qid), query, int(doc_id[6:]), doc, int(label)))
            qid, query, doc_id, title, doc, _, label = line.strip().split("\t")  # event_train
            docs.append((int(qid), query, int(doc_id), doc, int(label)))  # event_train
            # qid, query, doc_id, title, doc, _, _, label = line.strip().split("\t")  # justevent_train
            # docs.append((int(qid), query, int(doc_id), doc, int(label)))  # justevent_train
    return docs


def tokenize_query(query):
    query = query.lower().strip()
    return nltk.tokenize.word_tokenize(query)[:max_tokens]


def tokenize_doc(doc):
    doc_tokens = []
    doc = doc.lower().strip().replace(',', ' ')  # 去掉,
    sents = nltk.tokenize.sent_tokenize(doc)
    for sent in sents:
        if len(doc_tokens) >= max_sents:
            break
        tokens = nltk.tokenize.word_tokenize(sent)

        # 添加去除停用词
        # tokens = [word for word in tokens if word not in stopwords.words("english")]

        splitted_num = len(tokens)//max_tokens if len(tokens) % max_tokens == 0 else len(tokens)//max_tokens + 1
        for i in range(splitted_num):
            doc_tokens.append(tokens[int(i*max_tokens):int((i+1)*max_tokens)])
    return doc_tokens[:max_sents]


# 生成word->id的映射词典
def gen_vocab(file_path, dump_path, min_freq=1):
    long_word_list, qid_set = list(), set()
    for qid, query, _, doc, _ in load_docs(file_path):
        if qid not in qid_set:
            long_word_list += tokenize_query(query)
            qid_set.add(qid)
        # sents = tokenize_doc(doc)  # 以句子划分
        sents = seg_passage.seg_pa(doc, m, n)  # 以passage划分
        for sent in sents:
            long_word_list += sent
    counter = Counter(long_word_list)
    words = list(filter(lambda x: x[1] >= min_freq, counter.most_common()))
    word_dict = {"<pad>": 0, "<unk>": 1}
    for word, _ in words:
        word_dict[word] = len(word_dict)
    with open(dump_path, "w+", encoding="utf-8") as f:
        json.dump(word_dict, f)
    return word_dict


# 读取glove的词向量
def load_glove(glove_path, dump_path, word_dict):
    def turn_str_to_vec(tokens):
        return torch.tensor([float(t) for t in tokens], dtype=torch.float)
    raw_emb = torch.empty((len(word_dict), d_emb), dtype=torch.float).uniform_(-0.1, 0.1) 
    words = set(word_dict.keys())
    regex = re.compile(r"(\S+)")
    with open(glove_path, 'r') as f:
        for line in f:
            obj = regex.match(line)
            if obj is None:
                continue
            if obj.group(0) in words:
                splits = line.strip().split()
                word, str_vec = splits[0], splits[1:]
                idx = word_dict[word]
                vec = turn_str_to_vec(str_vec)
                raw_emb[idx, :] = vec
    torch.save(raw_emb, dump_path)
    return raw_emb


def complement_doc(docs):
    max_size = max([len(doc) for doc in docs])
    comp_docs = []
    for doc in docs:
        comp_docs.append(doc + [[]] * (max_size - len(doc)))
    return comp_docs


def complement_sent(sentences):
    max_len = max([len(s) for s in sentences])
    comp_sents = []
    for s in sentences:
        comp_sents.append(s + [pad_id] * (max_len - len(s)))
    return comp_sents


class TrainLoader:
    def __init__(self, file_path, vocab, device):
        docs = load_docs(file_path)
        self.indexes = []
        self.data = {}
        self.vocab = vocab
        self.device = torch.device(device)

        for qid, query, _, doc, label in docs:
            query = self.tokens_to_ids(tokenize_query(query))
            doc = [self.tokens_to_ids(sent) for sent in tokenize_doc(doc)]
            if qid not in self.data:
                self.data[qid] = {"q": query, "p": [], "n": []}
            if label == 0:
                self.data[qid]["n"].append(doc)
            elif label == 1:
                self.indexes.append((qid, len(self.data[qid]["p"])))
                self.data[qid]["p"].append(doc)
        
    def tokens_to_ids(self, tokens):
        return [self.vocab[token] if token in self.vocab else unk_id for token in tokens]
    
    def pack(self, query, p_doc, n_docs):
        query = [query] * (1 + len(n_docs))
        docs = complement_doc([p_doc] + n_docs)
        segments = [complement_sent(seg) for seg in list(zip(*docs))]
        return (
            torch.tensor(query).long().to(self.device),
            [torch.tensor(seg).long().to(self.device) for seg in segments]
        )
        
    def __call__(self):  # 查询，正例与负例打包
        random.shuffle(self.indexes)
        for qid, doc_idx in self.indexes:
            query = self.data[qid]["q"]
            p_doc = self.data[qid]["p"][doc_idx]
            n_docs = list(random.choices(self.data[qid]["n"], k=n_p))
            yield self.pack(query, p_doc, n_docs)

class TestLoader:
    def __init__(self, file_path, vocab, device):
        docs = load_docs(file_path)
        self.data = {}
        self.vocab = vocab
        self.device = torch.device(device)
        
        for qid, query, doc_id, doc, label in docs:
            query = self.tokens_to_ids(tokenize_query(query))
            doc = [self.tokens_to_ids(sent) for sent in tokenize_doc(doc)]
            if qid not in self.data:
                self.data[qid] = {"q": query, "d": []}
            self.data[qid]["d"].append((doc_id, doc, label))

    def tokens_to_ids(self, tokens):
        return [self.vocab[token] if token in self.vocab else unk_id for token in tokens]
    
    def pack(self, query, docs, labels, qid, doc_ids):
        query = [query] * len(docs)
        docs = complement_doc(docs)
        segments = [complement_sent(seg) for seg in list(zip(*docs))]
        return (
            torch.tensor(query).long().to(self.device),
            [torch.tensor(seg).long().to(self.device) for seg in segments],
            torch.tensor(labels).long().to(self.device),
            qid,
            list(doc_ids)
        )
    
    def __call__(self):
        for qid in self.data:
            obj = self.data[qid]
            query = obj["q"]
            doc_ids, docs, labels = zip(*obj["d"])
            yield self.pack(query, docs, labels, qid, doc_ids)


if __name__ == "__main__":
    word_dict = gen_vocab("data/event_train.csv", "dump/vocab.json", min_freq=1)
    load_glove("data/glove.6B.300d.txt", "dump/glove.emb", word_dict)