#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2011 Radim Rehurek <radimrehurek@seznam.cz>


"""
Server for vector space "find similar" service, using gensim as back-end.

The server performs 3 main functions:

1. converts documents to semantic representation
2. indexes documents in the semantic representation, for faster retrieval
3. for a given query document, returns the most similar documents from the index

"""

from __future__ import with_statement

import sys
import os
import logging
import random
import tempfile

import numpy

import gensim
from sqlitedict import SqliteDict


logger = logging.getLogger('gensim_server')



MODEL_METHOD = 'lsi' # use LSI to represent documents
#MODEL_METHOD = 'tfidf'
LSI_TOPICS = 400


def simple_preprocess(doc):
    """
    Convert a document into a list of tokens.

    This lowercases, tokenizes, stems, normalizes etc. -- the output are final,
    utf8 encoded strings that won't be processed any further.
    """
    tokens = [token.encode('utf8') for token in gensim.utils.tokenize(doc, lower=True, errors='ignore')
            if 2 <= len(token) <= 15 and not token.startswith('_')]
    return tokens


def merge_sim(oldsims, newsims):
    """Update precomputed similarities with new values."""
    return sorted(oldsims + newsims, key=lambda item:-item[1])[: SimIndex.TOP_SIMS]


class SimIndex(gensim.utils.SaveLoad):
    """
    An index of documents. Used internally by SimServer.

    Uses Similarity to persist the underlying document vectors to disk (via mmap).
    """
    SHARD_SIZE = 50000 # spill index shards to disk in SHARD_SIZE-ed chunks of documents
    TOP_SIMS = 100 # Only consider this many "most similar" documents, to speed up querying. Set to None for no clipping

    def __init__(self, fname, precompute=True):
        self.fname = fname
        self.id2pos = {}
        self.pos2id = {}
        self.id2sims = {}
        self.precompute = precompute
        self.length = 0


    def index_documents(self, fresh_docs, model):
        """
        Update index with new documents (potentially replacing old ones with
        the same id). `fresh_docs` is a dictionary-like object (=sqlitedict or dict)
        that maps document_id->document.

        """
        if not hasattr(self, 'qindex'):
            # this is the very first indexing call; create an empty index
            self.qindex = gensim.similarities.Similarity(self.fname, corpus=None,
                num_best=None, num_features=model.num_features, shardsize=SimIndex.SHARD_SIZE)

        docids = fresh_docs.keys()
        vectors = (model.docs2vecs(fresh_docs[docid] for docid in docids))

        logger.info("indexing %i new documents for %s" % (len(docids), self.fname))
        if not self.precompute:
            # we're not precomputing the "most similar" documents; just add the
            # vectors to the index and we're done
            self.qindex.add_documents(vectors)
            self.qindex.save()
            self.updateids(docids)
        else:
            # first, create a separate index only with the new documents
            logger.debug("adding %i documents to temporary index" % len(docids))
            tmpindex = gensim.similarities.Similarity(None, corpus=None,
                num_best=self.qindex.num_best, num_features=self.qindex.num_features, shardsize=self.qindex.shardsize)
            tmpindex.add_documents(vectors)
            tmpindex.close_shard()
            tmpindex.normalize = False

            # update precomputed "most similar" for old documents (in case some of
            # the new docs make it to the top-N for some of the old documents)
            logger.debug("updating old precomputed values")
            pos = 0
            for chunk in self.qindex.iter_chunks():
                for sims in tmpindex[chunk]:
                    docid = self.pos2id[pos]
                    sims = self.sims2scores(sims)
                    self.id2sims[docid] = merge_sim(self.id2sims[docid], sims)
                    pos += 1

            # add the tmpindex to qindex
            logger.debug("merging temporary index into permanent one")
            pos = 0
            for chunk in tmpindex.iter_chunks():
                self.qindex.add_documents(chunk)
            self.qindex.save()
            self.updateids(docids)

            # precompute "most similar" for the newly added documents
            logger.debug("precomputing values for the new documents")
            pos = 0
            norm, self.qindex.normalize = self.qindex.normalize, False
            for chunk in tmpindex.iter_chunks():
                for sims in self.qindex[chunk]:
                    docid = docids[pos]
                    self.id2sims[docid] = self.sims2scores(sims)
                    pos += 1
            self.qindex.normalize = norm

            # now the temporary index of new documents has been fully merged; clean up
            del tmpindex # TODO delete all created temp files!
        #endif

    def updateids(self, docids):
        logger.info("updating %i id mappings" % len(docids))
        for docid in docids:
            # update position->id mappings
            if docid in self.id2pos:
                logger.info("replacing existing document %r in index %s" % (docid, self.fname))
                del self.pos2id[self.id2pos[docid]]
            self.id2pos[docid] = self.length
            self.pos2id[self.length] = docid
            self.length += 1


    def delete(self, docids):
        logger.info("deleting %i documents from %s" % (len(docids), self.fname))
        for docid in docids:
            del self.id2pos[docid]
        self.pos2id = dict((v, k) for k, v in self.id2pos.iteritems())
        assert len(self.pos2id) == len(self.id2pos), "duplicate ids or positions detected"


    def sims2scores(self, sims):
        result = []
        sims = abs(sims) # TODO or maybe clip? are opposite vectors "similar" or "dissimilar"?!
        for pos in numpy.argsort(sims)[::-1]:
            if pos in self.pos2id: # ignore deleted/rewritten documents
                # convert positions of resulting docs back to ids
                result.append((self.pos2id[pos], sims[pos]))
                if len(result) == SimIndex.TOP_SIMS:
                    break
        return result


    def sims_by_id(self, docid):
        # convert document id to internal position and perform the query
        if self.precompute:
            result = self.id2sims[docid]
        else:
            sims = self.qindex.similarity_by_id(self.id2pos[docid])
            result = self.sims2scores(sims)
        return result


    def sims_by_doc(self, doc, model):
        # convert document (text) to vector
        vec = model.doc2vec(doc)
        # query the index
        sims = self.qindex[vec]
        return self.sims2scores(sims)


    def __len__(self):
        return len(self.id2pos)


    def __str__(self):
        return "SimIndex(%i docs, %i real size)" % (len(self), self.length)
#endclass SimIndex



class SimModel(gensim.utils.SaveLoad):
    """
    A semantic model responsible for translating between plain text and (semantic)
    vectors.

    These vectors can then be indexed/queried for similarity, see the `EudmlIndex`
    class.

    Currently uses the Latent Semantic Analysis over tf-idf representation of documents.

    """
    def __init__(self, fresh_docs, dictionary=None, method=MODEL_METHOD, preprocess=simple_preprocess):
        """
        Train a model, using `fresh_docs` as training corpus.

        If `dictionary` is not specified, it is computed from the documents.

        `method` is currently one of "tfidf"/"lsi"/"lda".

        `preprocess` is a function that takes a text and returns a sequence of
        preprocessed tokens. It is used to parse documents.
        """
        self.preprocess = preprocess
        self.method = method # TODO: use subclassing/injection for different methods, instead of param?
        docids = fresh_docs.keys()
        random.shuffle(docids)
        logger.info("creating model from %s documents" % len(docids))

        logger.info("preprocessing texts")
        preprocessed = SqliteDict(gensim.utils.randfname(prefix='gensim'))
        for docid in docids:
            preprocessed[docid] = self.preprocess(fresh_docs[docid]['text'])

        # create id->word (integer->string) mapping
        logger.info("creating dictionary from %s documents" % len(fresh_docs))
        if dictionary is None:
            self.dictionary = gensim.corpora.Dictionary(preprocessed.itervalues())
            if len(fresh_docs) >= 1000:
                self.dictionary.filter_extremes(no_below=5, no_above=0.2, keep_n=50000)
            else:
                logger.warning("training model on only %i documents; is this intentional?" % len(fresh_docs))
                self.dictionary.filter_extremes(no_below=2, no_above=0.5, keep_n=50000)
        else:
            self.dictionary = dictionary

        if method == 'lsi':
            logger.info("training TF-IDF model")
            corpus = (self.dictionary.doc2bow(text) for text in preprocessed.itervalues())
            self.tfidf = gensim.models.TfidfModel(corpus, id2word=self.dictionary)
            logger.info("training LSI model")
            corpus = (self.dictionary.doc2bow(text) for text in preprocessed.itervalues())
            tfidf_corpus = self.tfidf[corpus]
            self.lsi = gensim.models.LsiModel(tfidf_corpus, id2word=self.dictionary, num_topics=LSI_TOPICS)
            self.num_features = len(self.lsi.projection.s)
        else:
            msg = "unknown semantic method %s" % method
            logger.error(msg)
            raise NotImplementedError(msg)


    def doc2vec(self, doc):
        """Convert a single SimilarityDocument to vector."""
        # TODO take method into account
        tokens = self.preprocess(doc['text'])
        bow = self.dictionary.doc2bow(tokens)
        tfidf = self.tfidf[bow]
        lsi = self.lsi[tfidf]
        return lsi


    def docs2vecs(self, docs):
        """Convert multiple SimilarityDocuments to vectors (batch version of doc2vec)."""
        bow = (self.dictionary.doc2bow(self.preprocess(doc['text'])) for doc in docs)
        tfidf = self.tfidf[bow]
        lsi = self.lsi[tfidf]
        return lsi


    def __str__(self):
        return "SimModel(method=%s, dict=%s)" % (self.method, self.dictionary)
#endclass SimModel



class SimServer(gensim.utils.SaveLoad):
    """
    This is top-level functionality for the similarity services. It takes care of
    indexing/creating models/querying.

    An object of this class can be shared across network via Pyro, to answer remote
    client requests.
    """
    def __init__(self, basename):
        """All data will be stored under `basename` (a filename prefix)."""
        self.basename = basename
        self.simindex = None
        self.simmodel = None
        self.fresh_docs = {}
        self.flush()


    def flush(self, delete_fresh=True):
        """
        Commit all changes, clear all caches. If `delete_fresh`, also clear the
        `add_documents()` cache.
        """
        # erase all temporary documents
        if delete_fresh:
            self.fresh_docs = {}
        self.save(self.basename)


    def train(self, corpus=None, method='lsi'):
        """
        Create an indexing model. Will overwrite the model if it already exists.

        The model is trained on documents previously entered via `add_documents`,
        or directly on `corpus`, if specified.
        """
        if corpus is not None:
            self.flush(delete_fresh=True)
            self.add_documents(corpus)
            del corpus
        self.simmodel = SimModel(self.fresh_docs)
        self.flush(delete_fresh=True)


    def index(self, corpus=None):
        """
        Permanently index all documents previously added via `add_documents`, or
        directly documents from `corpus`, if specified.

        The indexing model must already exist (see `train`) before this function
        is called.
        """
        if not self.simmodel:
            msg = 'must initialize the model for %s before indexing documents' % self.basename
            logger.error(msg)
            raise AttributeError(msg)

        if corpus is not None:
            self.flush(delete_fresh=True)
            self.add_documents(corpus)
            del corpus

        if not self.simindex:
            logger.info("starting a new index for %s" % self.basename)
            self.simindex = SimIndex(self.basename + ".index")
        self.simindex.index_documents(self.fresh_docs, self.simmodel)
        self.flush(delete_fresh=True)


    def add_documents(self, documents):
        """
        Add a sequence of documents to be processed (indexed or trained on).

        Here, the documents are simply collected; real processing is done later,
        during the `self.index` or `self.train` calls.

        `add_documents` can be called repeatedly; the result is the same as if
        it was called once, with a concatenation of all the partial document batches.
        The point is to save memory when sending large corpora over network: the
        entire `documents` must be serialized into RAM.

        A call to `flush()` clears this documents-to-be-processed buffer (`flush`
        is implicitly called when you call `index()` and `train()`).
        """
        for doc in documents:
            docid = doc['id']
            logger.debug("buffering document %r" % docid)
            if docid in self.fresh_docs:
                logger.warning("asked to re-add id %r; rewriting old value" % docid)
            self.fresh_docs[docid] = doc


    def find_similar(self, doc, min_score=0.0, max_results=100):
        """
        Find at most `max_results` most similar articles to the article `doc`,
        each having similarity score of at least `min_score`.

        `doc` is either a string (document id, previously indexed) or a
        SimilarityDocument-like object with a 'text' attribute. This text is
        processed to produce a vector, which is then used as a query.

        The similar documents are returned in decreasing similarity order, as
        (doc_id, doc_score) tuples.
        """
        logger.debug("received query call with %r" % doc)
        if isinstance(doc, basestring):
            # query by direct document id
            sims = self.simindex.sims_by_id(doc)
        else:
            # query by an arbitrary text (=string) inside doc['text']
            sims = self.simindex.sims_by_doc(doc, self.simmodel)

        result = []
        for docid, score in sims:
            if score < min_score or 0 < max_results <= len(result):
                break
            result.append((docid, score))
        return result


    def drop_index(self, keep_model=False):
        self.simindex = None
        if not keep_model:
            self.simmodel = None
        self.flush(delete_fresh=True)


    def __str__(self):
        return "SimServer(loc=%r, index=%s, model=%s)" % (self.basename, self.simindex, self.simmodel)


    def status(self):
        return str(self)
#endclass SimServer
