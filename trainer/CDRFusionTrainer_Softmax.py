# coding: utf-8
from typing import Iterator, Dict
import numpy as np
import random
from collections import defaultdict
import h5py

from trainer.CDRBaseTrainer_Softmax import CDRBaseTrainer_Softmax
from trainer.CDRBaseTrainer_Softmax import max_num_mentions_per_example, \
    IGNORE_INDEX, mentions_limit, max_num_mentions_per_entity, test_mentions_limit


class CDRFusionTrainer_Softmax(CDRBaseTrainer_Softmax):

    def __init__(self, params) -> None:
        super(CDRFusionTrainer_Softmax, self).__init__(params)

    def assemble_relation(self, head_mentions_indices, head_mentions_indices_mask,
                          tail_mentions_indices, tail_mentions_indices_mask,
                          ht_comb_indices, ht_pair_pos, ht_comb_mask,
                          ii, jj, span_pos_to_idx, hlist, tlist, limit=max_num_mentions_per_entity):
        num_H, num_T = 0, 0
        for hh in hlist[:limit]:
            head_mentions_indices[ii, jj, num_H] = span_pos_to_idx[f"{hh['pos'][0]}_{hh['pos'][1]}"]
            head_mentions_indices_mask[ii, jj, num_H] = 1
            num_H += 1
        for tt in tlist[:limit]:
            tail_mentions_indices[ii, jj, num_T] = span_pos_to_idx[f"{tt['pos'][0]}_{tt['pos'][1]}"]
            tail_mentions_indices_mask[ii, jj, num_T] = 1
            num_T += 1
        # h1,h2; t1,t2,t3 :
        # ht_comb_indices[..., 0]: h1 h1 h1 h2 h2 h2
        # ht_comb_indices[..., 1]: t1 t2 t3 t1 t2 t3
        kk = 0
        for index_h, hh in enumerate(hlist[:limit]):
            for index_t, tt in enumerate(tlist[:limit]):
                ht_comb_indices[ii, jj, kk, 0] = index_h
                ht_comb_indices[ii, jj, kk, 1] = index_t
                ht_pair_pos[ii, jj, kk] = \
                    self.get_head_tail_relative_pos(hh, tt)
                ht_comb_mask[ii, jj, kk] = 1
                kk += 1
        return num_H, num_T, kk

    def assemble_mentions(self, entity_span_indices, batch_example_idx, vertexSet):
        span_pos_to_idx = {}
        num_entities = 0
        for vertex_idx, entity in enumerate(vertexSet):
            for _e_idx, span in enumerate(entity):
                span_unique_pos = f"{span['pos'][0]}_{span['pos'][1]}"
                span_pos_to_idx[span_unique_pos] = num_entities
                entity_span_indices[batch_example_idx, num_entities, 0] = span['pos'][0]
                entity_span_indices[batch_example_idx, num_entities, 1] = span['pos'][1] - 1  # to include end index
                num_entities += 1
        return span_pos_to_idx, num_entities

    def get_train_batch(self) -> Iterator[Dict]:
        random.shuffle(self.train_order)
        context_idxs = np.zeros((self.batch_size, self.max_length), dtype=np.int64)
        if self.use_bert_embedding:
            bert_embeddings = np.zeros((self.batch_size, self.max_length, self.bert_embedd_dim), dtype=np.float32)
        context_pos = np.zeros((self.batch_size, self.max_length), dtype=np.int16)
        relation_multi_label = np.zeros((self.batch_size, self.train_h_t_limit, self.relation_num), dtype=np.float32)
        relation_mask = np.zeros((self.batch_size, self.train_h_t_limit), dtype=np.float32)
        context_ner = np.zeros((self.batch_size, self.max_length), dtype=np.int16)
        relation_label = np.zeros((self.batch_size, self.train_h_t_limit), dtype=np.int16)
        entity_span_indices = np.zeros((self.batch_size, max_num_mentions_per_example, 2), dtype=np.int16)
        # head_tail_indices [..., 0] for head, [..., 1] for tail
        head_mentions_indices = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit), dtype=np.int16)
        head_mentions_indices_mask = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit), dtype=np.int16)
        tail_mentions_indices = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit), dtype=np.int16)
        tail_mentions_indices_mask = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit), dtype=np.int16)

        ht_comb_indices = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit * mentions_limit, 2), dtype=np.int16)
        ht_pair_pos = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit * mentions_limit), dtype=np.int16)
        ht_comb_mask = np.zeros((self.batch_size, self.train_h_t_limit, mentions_limit * mentions_limit), dtype=np.int16)

        num_pos_entity_pairs, num_neg_entity_pairs, num_entity_pairs = 0, 0, 0
        with h5py.File(self.train_bert_feature, 'r') as fin:
            for b in range(self.train_batches):
                start_id = b * self.batch_size
                cur_bsz = min(self.batch_size, self.train_len - start_id)
                cur_batch = list(self.train_order[start_id: start_id + cur_bsz])
                cur_batch.sort(key=lambda x: np.sum(self.data_train_word[x] > 0), reverse=True)

                for _tensor in [entity_span_indices, head_mentions_indices, tail_mentions_indices,
                                tail_mentions_indices_mask, head_mentions_indices_mask, ht_comb_mask,
                                ht_comb_indices, relation_multi_label, relation_mask, ht_pair_pos]:
                    _tensor.fill(0)

                # pos_idx.fill(0)
                relation_label.fill(IGNORE_INDEX)

                batch_max_h_t_cnt = 1
                batch_lens, indexes = [], []
                batch_max_num_entities = 1
                batch_max_num_H, batch_max_num_T = 1, 1
                batch_max_num_combination = 1

                for batch_example_idx, index in enumerate(cur_batch):
                    context_idxs[batch_example_idx] = self.data_train_word[index, :]
                    context_pos[batch_example_idx] = self.data_train_pos[index, :]
                    # context_char_idxs[batch_example_idx] = self.data_train_char[index, :]
                    context_ner[batch_example_idx] = self.data_train_ner[index, :]

                    if self.use_bert_embedding:
                        seq_embedd = np.array(fin[str(index)])
                        batch_lens.append(seq_embedd.shape[0])
                        bert_embeddings[batch_example_idx] = self.padding(seq_embedd, self.max_length, self.bert_embedd_dim)

                    # for example_rel_idx in range(self.max_length):
                    #     if self.data_train_word[index, example_rel_idx] == 0:
                    #         break
                    #     pos_idx[batch_example_idx, example_rel_idx] = example_rel_idx + 1

                    ins = self.train_file[index]
                    labels = ins['labels']
                    idx2label = defaultdict(list)

                    for label in labels:
                        idx2label[(label['h'], label['t'])].append(label['r'])

                    span_pos_to_idx, num_entities = self.assemble_mentions(entity_span_indices,
                                                                           batch_example_idx, ins['vertexSet'])
                    batch_max_num_entities = max(batch_max_num_entities, num_entities)

                    train_tripe = list(idx2label.keys())

                    rel_cnt = 0
                    positive_is_enough = False
                    for _, (h_idx, t_idx) in enumerate(train_tripe):
                        example_rel_idx = rel_cnt
                        hlist = ins['vertexSet'][h_idx]
                        tlist = ins['vertexSet'][t_idx]

                        # real head-span end-span pairs
                        random.shuffle(hlist)
                        random.shuffle(tlist)
                        num_H, num_T, num_relation_comb = \
                            self.assemble_relation(head_mentions_indices,
                                                   head_mentions_indices_mask,
                                                   tail_mentions_indices,
                                                   tail_mentions_indices_mask,
                                                   ht_comb_indices, ht_pair_pos,
                                                   ht_comb_mask, batch_example_idx, example_rel_idx,
                                                   span_pos_to_idx, hlist, tlist, mentions_limit)
                        batch_max_num_H = max(batch_max_num_H, num_H)
                        batch_max_num_T = max(batch_max_num_T, num_T)
                        batch_max_num_combination = max(batch_max_num_combination, num_relation_comb)

                        label = idx2label[(h_idx, t_idx)]
                        for r in label:
                            relation_multi_label[batch_example_idx, example_rel_idx, r] = 1

                        relation_mask[batch_example_idx, example_rel_idx] = 1
                        rt = np.random.randint(len(label))
                        relation_label[batch_example_idx, example_rel_idx] = label[rt]
                        # this positive rel example ends
                        rel_cnt += 1
                        if rel_cnt == self.train_h_t_limit:
                            positive_is_enough = True
                            break

                    num_pos_entity_pairs += rel_cnt
                    random.shuffle(ins['na_triple'])
                    if self.use_neg_sample:
                        lower_bound = min(len(ins['na_triple']), len(train_tripe)*self.neg_sample_multiplier)
                    else:
                        lower_bound = len(ins['na_triple'])

                    neg_counter = 0
                    if not positive_is_enough:
                        for _, (h_idx, t_idx) in enumerate(ins['na_triple'][:lower_bound], len(train_tripe)):
                            example_rel_idx = rel_cnt
                            hlist = ins['vertexSet'][h_idx]
                            tlist = ins['vertexSet'][t_idx]

                            if self.train_hypernym_filter:
                                neg = [str(ins['title']), hlist[0]['KB_ID'], tlist[0]['KB_ID']]
                                if neg in self.negs_to_remove:
                                    continue

                            # Na head-span end-span pairs
                            random.shuffle(hlist)
                            random.shuffle(tlist)
                            num_H, num_T, num_relation_comb = \
                                self.assemble_relation(head_mentions_indices,
                                                       head_mentions_indices_mask,
                                                       tail_mentions_indices,
                                                       tail_mentions_indices_mask,
                                                       ht_comb_indices, ht_pair_pos,
                                                       ht_comb_mask, batch_example_idx, example_rel_idx,
                                                       span_pos_to_idx, hlist, tlist, mentions_limit)
                            batch_max_num_H = max(batch_max_num_H, num_H)
                            batch_max_num_T = max(batch_max_num_T, num_T)
                            batch_max_num_combination = max(batch_max_num_combination, num_relation_comb)

                            relation_multi_label[batch_example_idx, example_rel_idx, 0] = 1
                            relation_label[batch_example_idx, example_rel_idx] = 0
                            relation_mask[batch_example_idx, example_rel_idx] = 1

                            # this negative rel example ends
                            rel_cnt += 1
                            neg_counter += 1
                            if rel_cnt == self.train_h_t_limit:
                                break

                    indexes.append(index)
                    batch_max_h_t_cnt = max(batch_max_h_t_cnt, rel_cnt)
                    num_entity_pairs += rel_cnt
                    num_neg_entity_pairs += neg_counter
                    assert num_pos_entity_pairs + num_neg_entity_pairs == num_entity_pairs

                input_mask = (context_idxs[:cur_bsz] > 0).astype(np.bool)
                input_lengths = np.sum(input_mask, axis=1)
                max_c_len = int(np.max(input_lengths))
                if self.use_bert_embedding:
                    assert input_lengths.tolist() == batch_lens, 'len ERROR'

                input_mask = context_idxs[:cur_bsz, :max_c_len] > 0

                # add dummy mask for padding rel to prevent from `/ 0` or `nan`
                head_mentions_indices_mask[:, :, 0] = 1
                tail_mentions_indices_mask[:, :, 0] = 1
                ht_comb_mask[:, :, 0] = 1

                assert np.min(np.sum(head_mentions_indices_mask, axis=2)) >= 1, \
                    "head_mentions_indices_mask.sum(dim=2).min().item() <1"
                assert np.min(np.sum(head_mentions_indices_mask, axis=2)) >= 1, \
                    "tail_mentions_indices_mask.sum(dim=2).min().item() <1"

                yield {
                    'context_idxs': self._to_tensor(context_idxs[:cur_bsz, :max_c_len], 'long'),
                    'bert_feature': self._to_tensor(bert_embeddings[:cur_bsz, :max_c_len, :]) if self.use_bert_embedding else None,
                    'context_pos': self._to_tensor(context_pos[:cur_bsz, :max_c_len], 'long'),
                    'for_relation_repr': {
                        'entity_span_indices': self._to_tensor(entity_span_indices[:cur_bsz, :batch_max_num_entities, :], 'long'),
                        'head_mentions_indices': self._to_tensor(head_mentions_indices[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_H], 'long'),
                        'head_mentions_indices_mask': self._to_tensor(head_mentions_indices_mask[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_H], 'long'),
                        'tail_mentions_indices': self._to_tensor(tail_mentions_indices[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_T], 'long'),
                        'tail_mentions_indices_mask': self._to_tensor(tail_mentions_indices_mask[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_T], 'long'),
                        'ht_comb_indices': self._to_tensor(ht_comb_indices[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_combination, :], 'long'),
                        'ht_comb_mask': self._to_tensor(ht_comb_mask[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_combination], 'long'),
                    },
                    'relation_label': self._to_tensor(relation_label[:cur_bsz, :batch_max_h_t_cnt], 'long'),
                    # 'input_lengths': self._to_tensor(input_lengths, 'long'),
                    'input_mask': self._to_tensor(input_mask, 'bool'),
                    # 'pos_idx': self._to_tensor(pos_idx[:cur_bsz, :max_c_len], 'long'),
                    'pos_idx': None,
                    'relation_multi_label': self._to_tensor(relation_multi_label[:cur_bsz, :batch_max_h_t_cnt]),
                    'relation_mask': self._to_tensor(relation_mask[:cur_bsz, :batch_max_h_t_cnt]),
                    'context_ner': self._to_tensor(context_ner[:cur_bsz, :max_c_len], 'long'),
                    # 'context_char_idxs': self._to_tensor(context_char_idxs[:cur_bsz, :max_c_len], 'long'),
                    'context_char_idxs': None,
                    'ht_pair_pos': self._to_tensor(ht_pair_pos[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_combination], 'long'),
                    'indexes': indexes
                }

        self.num_train_entity_pairs = num_entity_pairs
        self.num_pos_entity_pairs = num_pos_entity_pairs
        self.num_neg_entity_pairs = num_neg_entity_pairs
        self.print_train_num_pos_neg()

    def get_test_batch(self) -> Iterator[Dict]:
        context_idxs = np.zeros((self.test_batch_size, self.max_length), dtype=np.int64)
        if self.use_bert_embedding:
            bert_embeddings = np.zeros((self.test_batch_size, self.max_length, self.bert_embedd_dim), dtype=np.float32)
        context_pos = np.zeros((self.test_batch_size, self.max_length), dtype=np.int16)
        context_ner = np.zeros((self.test_batch_size, self.max_length), dtype=np.int16)
        # context_char_idxs = np.zeros((self.test_batch_size, self.max_length, self.char_limit), dtype=np.int16)
        relation_mask = np.zeros((self.test_batch_size, self.test_relation_limit), dtype=np.float32)

        # entity_span_indices
        # last dim `0` for start, `1` for end (inclusive)
        entity_span_indices = np.zeros((self.test_batch_size, max_num_mentions_per_example, 2), dtype=np.int16)
        # test_coref_limit = coref_limit  # max_num_coref_per_vertex
        head_mentions_indices = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit), dtype=np.int16)
        head_mentions_indices_mask = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit), dtype=np.int16)
        tail_mentions_indices = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit), dtype=np.int16)
        tail_mentions_indices_mask = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit), dtype=np.int16)

        # max_num_coref_per_vertex = 23
        # num_combinations = max_num_coref_per_vertex*max_num_coref_per_vertex
        ht_comb_indices = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit * test_mentions_limit, 2), dtype=np.int16)
        ht_pair_pos = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit * test_mentions_limit), dtype=np.int16)
        ht_comb_mask = np.zeros((self.test_batch_size, self.test_relation_limit, test_mentions_limit * test_mentions_limit), dtype=np.int16)

        num_entity_pairs = 0
        with h5py.File(self.test_bert_feature, 'r') as fin:
            for b in range(self.test_batches):
                start_id = b * self.test_batch_size
                cur_bsz = min(self.test_batch_size, self.test_len - start_id)
                cur_batch = list(self.test_order[start_id: start_id + cur_bsz])

                for _tensor in [entity_span_indices, ht_pair_pos, relation_mask, head_mentions_indices,
                                tail_mentions_indices, tail_mentions_indices_mask, head_mentions_indices_mask,
                                ht_comb_mask, ht_comb_indices]:
                    _tensor.fill(0)

                batch_max_h_t_cnt = 1
                batch_max_num_entities = 1
                batch_max_num_H, batch_max_num_T = 1, 1
                batch_max_num_combination = 1

                cur_batch.sort(key=lambda x: np.sum(self.data_test_word[x] > 0), reverse=True)

                labels, L_vertex, titles, indexes, batch_lens = [], [], [], [], []
                na_triples = []
                for batch_example_idx, index in enumerate(cur_batch):
                    context_idxs[batch_example_idx] = self.data_test_word[index, :]
                    context_pos[batch_example_idx] = self.data_test_pos[index, :]
                    # context_char_idxs[batch_example_idx] = self.data_test_char[index, :]
                    context_ner[batch_example_idx] = self.data_test_ner[index, :]

                    if self.use_bert_embedding:
                        seq_embedd = np.array(fin[str(index)])
                        batch_lens.append(seq_embedd.shape[0])
                        bert_embeddings[batch_example_idx] = self.padding(seq_embedd, self.max_length, self.bert_embedd_dim)
                    ins = self.test_file[index]

                    span_pos_to_idx, num_entities = self.assemble_mentions(entity_span_indices,
                                                                           batch_example_idx, ins['vertexSet'])
                    batch_max_num_entities = max(batch_max_num_entities, num_entities)

                    L = len(ins['vertexSet'])
                    example_rel_idx = 0
                    for h_idx in range(L):
                        for t_idx in range(L):
                            hlist = ins['vertexSet'][h_idx]
                            tlist = ins['vertexSet'][t_idx]
                            h_type = hlist[0]['type']
                            t_type = tlist[0]['type']
                            assert h_type in ['Chemical', 'Disease']
                            assert t_type in ['Chemical', 'Disease']
                            if h_idx != t_idx and (h_type == 'Chemical' and t_type == 'Disease'):
                                if self.test_hypernym_filter:
                                    neg = [str(ins['title']), hlist[0]['KB_ID'], tlist[0]['KB_ID']]
                                    if neg in self.negs_to_remove:
                                        continue
                                num_H, num_T, num_relation_comb = \
                                    self.assemble_relation(head_mentions_indices,
                                                           head_mentions_indices_mask,
                                                           tail_mentions_indices,
                                                           tail_mentions_indices_mask,
                                                           ht_comb_indices, ht_pair_pos,
                                                           ht_comb_mask, batch_example_idx, example_rel_idx,
                                                           span_pos_to_idx, hlist, tlist, test_mentions_limit)
                                batch_max_num_H = max(batch_max_num_H, num_H)
                                batch_max_num_T = max(batch_max_num_T, num_T)
                                batch_max_num_combination = max(batch_max_num_combination, num_relation_comb)

                                relation_mask[batch_example_idx, example_rel_idx] = 1
                                example_rel_idx += 1
                                num_entity_pairs += 1

                    batch_max_h_t_cnt = max(batch_max_h_t_cnt, example_rel_idx)

                    titles.append(ins['title'])
                    label_set = {}
                    for label in ins['labels']:
                        label_set[(label['h'], label['t'], label['r'])] = label['in' + self.train_prefix]
                    labels.append(label_set)
                    L_vertex.append(L)
                    indexes.append(index)
                    na_triples.append(ins['na_triple'])

                input_mask = (context_idxs[:cur_bsz] > 0).astype(np.bool)
                input_lengths = np.sum(input_mask, axis=1)
                max_c_len = int(np.max(input_lengths))
                if self.use_bert_embedding:
                    assert input_lengths.tolist() == batch_lens, 'len ERROR'

                input_mask = context_idxs[:cur_bsz, :max_c_len] > 0

                # add dummy mask for padding rel to prevent from `/ 0` or `nan`
                head_mentions_indices_mask[:, :, 0] = 1
                tail_mentions_indices_mask[:, :, 0] = 1
                ht_comb_mask[:, :, 0] = 1

                assert np.min(np.sum(head_mentions_indices_mask, axis=2)) >= 1, \
                    "head_mentions_indices_mask.sum(dim=2).min().item() <1"
                assert np.min(np.sum(head_mentions_indices_mask, axis=2)) >= 1, \
                    "tail_mentions_indices_mask.sum(dim=2).min().item() <1"

                yield {
                    'context_idxs': self._to_tensor(context_idxs[:cur_bsz, :max_c_len], 'long'),
                    'bert_feature': self._to_tensor(bert_embeddings[:cur_bsz, :max_c_len, :]) if self.use_bert_embedding else None,
                    'context_pos': self._to_tensor(context_pos[:cur_bsz, :max_c_len], 'long'),
                    'for_relation_repr': {
                        'entity_span_indices': self._to_tensor(entity_span_indices[:cur_bsz, :batch_max_num_entities, :], 'long'),
                        'head_mentions_indices': self._to_tensor(head_mentions_indices[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_H], 'long'),
                        'head_mentions_indices_mask': self._to_tensor(head_mentions_indices_mask[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_H], 'long'),
                        'tail_mentions_indices': self._to_tensor(tail_mentions_indices[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_T], 'long'),
                        'tail_mentions_indices_mask': self._to_tensor(tail_mentions_indices_mask[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_T], 'long'),
                        'ht_comb_indices': self._to_tensor(ht_comb_indices[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_combination, :], 'long'),
                        'ht_comb_mask': self._to_tensor(ht_comb_mask[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_combination], 'long')
                    },
                    # 'input_lengths': self._to_tensor(input_lengths, 'long'),
                    'input_mask': self._to_tensor(input_mask, 'bool'),
                    'context_ner': self._to_tensor(context_ner[:cur_bsz, :max_c_len], 'long'),
                    # 'context_char_idxs': self._to_tensor(context_char_idxs[:cur_bsz, :max_c_len], 'long'),
                    'context_char_idxs': None,
                    'relation_mask': self._to_tensor(relation_mask[:cur_bsz, :batch_max_h_t_cnt]),
                    'ht_pair_pos': self._to_tensor(ht_pair_pos[:cur_bsz, :batch_max_h_t_cnt, :batch_max_num_combination], 'long'),
                    'indexes': indexes,
                    'titles': titles,
                    'L_vertex': L_vertex,
                    'labels': labels,
                    'na_triples': na_triples
                }

        self.num_test_entity_pairs = num_entity_pairs
        self.print_test_num_entity_pairs()

    def get_config_str(self) -> str:
        config_str = super(CDRFusionTrainer_Softmax, self).get_config_str()
        return config_str + self.get_format_params_end_line()
