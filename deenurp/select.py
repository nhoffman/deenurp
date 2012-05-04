"""
Select reference sequences for inclusion
"""
import collections
import csv
import itertools
import logging
import subprocess
import tempfile

from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from . import search
from .util import as_fasta, tempdir
from .wrap import cmalign, as_refpkg, redupfile_of_seqs, \
                  voronoi, guppy_redup, pplacer, esl_sfetch

DEFAULT_THREADS = 12
CLUSTER_THRESHOLD = 0.998

def seqrecord(name, residues, **annotations):
    sr = SeqRecord(Seq(residues), name)
    sr.annotations.update(annotations)
    return sr

def _cluster(sequences, threshold=CLUSTER_THRESHOLD):
    with as_fasta(sequences) as fp, tempfile.NamedTemporaryFile() as ntf:
        cmd = ['usearch', '-cluster', fp, '-seedsout', ntf.name, '-id',
                str(threshold),
                '-usersort',
                '-quiet', '-nowordcountreject']
        subprocess.check_call(cmd)
        r = frozenset(i.id for i in SeqIO.parse(ntf, 'fasta'))
    logging.info("Clustered %d to %d", len(sequences), len(r))
    return [i for i in sequences if i.id in r]

def select_sequences_for_cluster(ref_seqs, query_seqs, keep_leaves=5,
        threads=DEFAULT_THREADS, mpi_args=None):
    """
    Given a set of reference sequences and query sequences, select
    keep_leaves appropriate references.
    """
    # Cluster
    ref_seqs = _cluster(ref_seqs)
    if len(ref_seqs) <= keep_leaves:
        return [i.id for i in ref_seqs]

    c = itertools.chain(ref_seqs, query_seqs)
    ref_ids = frozenset(i.id for i in ref_seqs)
    aligned = list(cmalign(c, mpi_args=mpi_args))
    with as_refpkg((i for i in aligned if i.id in ref_ids), threads=threads) as rp, \
             as_fasta(aligned) as fasta, \
             tempdir(prefix='jplace') as placedir, \
             redupfile_of_seqs(query_seqs) as redup_path:

        jplace = pplacer(rp.path, fasta, out_dir=placedir(), threads=threads)
        # Redup
        guppy_redup(jplace, redup_path, placedir('redup.jplace'))
        prune_leaves = set(voronoi(placedir('redup.jplace'), keep_leaves))

    result = frozenset(i.id for i in ref_seqs) - prune_leaves

    assert len(result) == keep_leaves

    return result

def fetch_cluster_members(cluster_info_file):
    d = collections.defaultdict(list)
    with open(cluster_info_file) as fp:
        r = csv.DictReader(fp)
        for i in r:
            d[i['cluster']].append(i['seqname'])
    return d

def cluster_hit_seqs(con, cluster_name):
    sql = '''SELECT DISTINCT sequences.name, weight
        FROM sequences
        INNER JOIN best_hits USING (sequence_id)
        INNER JOIN ref_seqs USING(ref_id)
        WHERE cluster_name = ?'''
    cursor = con.cursor()
    cursor.execute(sql, [cluster_name])
    return list(cursor)

def esl_sfetch_seqs(sequence_file, sequence_names):
    with tempfile.NamedTemporaryFile(prefix='esl', suffix='.fasta') as tf:
        esl_sfetch(sequence_file, sequence_names, tf)
        tf.seek(0)
        return list(SeqIO.parse(tf, 'fasta'))

def get_total_weight(con):
    sql = 'SELECT SUM(weight) FROM sequences'
    cursor = con.cursor()
    cursor.execute(sql)
    return cursor.fetchone()[0]

def choose_references(deenurp_db, refs_per_cluster=5,
        threads=DEFAULT_THREADS, min_cluster_prop=0.0, mpi_args=None):
    """
    Choose reference sequences from a search, choosing refs_per_cluster
    reference sequences for each nonoverlapping cluster.
    """
    params = search.load_params(deenurp_db)
    fasta_file = params['fasta_file']
    ref_fasta = params['ref_fasta']
    total_weight = get_total_weight(deenurp_db)
    cluster_members = fetch_cluster_members(params['ref_cluster_names'])

    # Iterate over clusters
    cursor = deenurp_db.cursor()
    cursor.execute('''SELECT cluster_name, total_weight
            FROM vw_cluster_weights
            ORDER BY total_weight DESC''')

    for cluster_name, cluster_weight in cursor:
        cluster_seq_names = dict(cluster_hit_seqs(deenurp_db, cluster_name))
        cluster_refs = esl_sfetch_seqs(ref_fasta, cluster_members[cluster_name])
        query_seqs = esl_sfetch_seqs(fasta_file, cluster_seq_names)
        for i in query_seqs:
            i.annotations['weight'] = cluster_seq_names[i.id]

        if cluster_weight / total_weight < min_cluster_prop:
            logging.info("Skipping cluster %s. Total weight: %.3f%%",
                    cluster_name, cluster_weight / total_weight * 100)
            break

        logging.info('Cluster %s: %.3f%%, %d hits', cluster_name,
                cluster_weight / total_weight * 100, len(cluster_seq_names))
        ref_names = select_sequences_for_cluster(cluster_refs, query_seqs, mpi_args=mpi_args,
                keep_leaves=refs_per_cluster, threads=threads)
        refs = (i for i in cluster_refs if i.id in ref_names)
        for ref in refs:
            ref.annotations.update({'cluster_name': cluster_name,
                'weight_prop': cluster_weight/total_weight})
            yield ref
