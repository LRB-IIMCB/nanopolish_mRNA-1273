"""
reestimate_polya_emissions.py: given two `polya-samples` TSV files based on different
underlying kmer models (with the newer TSV giving failing poly(A) segmentations),
infer the best new parameters for the HMM emissions.

Usage:
$ python reestimate_polya_emissions.py samples.old.tsv seg.old.tsv samples.new.tsv
where:
* `samples.old.tsv` is the output of `nanopolish polya -vv [...] | grep 'polya-samples'`,
generated by the **old** kmer models;
* `seg.old.tsv` is the output of `nanopolish polya -v [...] | grep 'polya-segmentation'`,
generated by the **old** kmer models;
* `samples.new.tsv` is the output of `nanopolish polya -vv [...] | grep 'polya-samples'`,
generated by the **new** kmer models.

Dependencies:
* numpy >= 1.11.2
* scipy >= 0.18.1
* sklearn >= 0.18.1
"""
import csv
import numpy as np
import argparse
import os
from scipy.stats import norm
from sklearn.mixture import GaussianMixture


log_inv_sqrt_2pi = np.log(0.3989422804014327)
def log_normal_pdf(xs, mu, sigma):
    """Compute the log-normal PDF of a given sample(s) against a mu and sigma."""
    alpha = (xs - mu) * np.reciprocal(sigma)
    return ( log_inv_sqrt_2pi - np.log(sigma) + (-0.5 * alpha * alpha) )


def fit_gaussian(samples):
    """Given a numpy array of floating point samples, fit a gaussian distribution."""
    mu, sigma = norm.fit(samples)
    return (mu,sigma)


def fit_gmm(samples, ncomponents=2):
    """Given a numpy array of floating point samples, fit a gaussian mixture model."""
    # assume samples is of shape (NSAMPLES,); unsqueeze to (NSAMPLES,1) and train a GMM:
    gmm = GaussianMixture(n_components=ncomponents)
    gmm.fit(samples.reshape(-1,1))
    # return params of GMM in [(coeff, mu, sigma)] format:
    params = [(gmm.weights_[c], gmm.means_[c][0], gmm.covariances_[c][0][0]) for c in range(ncomponents)]
    return params


def old_tsv_to_numpy(tsv_path):
    """
    Read a TSV containing raw samples and return a dictionary consisting
    of the following numpy datasets:
    * S_loglkhd: the log-likelihoods of the samples belonging to the START segment.
    * L_loglkhd: the log-likelihoods of the samples belonging to the LEADER segment.
    * A_loglkhd: the log-likelihoods of the samples belonging to the ADAPTER segment.
    * P_loglkhd: the log-likelihoods of the samples belonging to the POLYA segment.
    * T_loglkhd: the log-likelihoods of the samples belonging to the TRANSCRIPT segment.
    """
    # instantiate arrays to hold values:
    S_loglkhd = []
    L_loglkhd = []
    A_loglkhd = []
    P_loglkhd = []
    T_loglkhd = []

    # loop over TSV file and append data to arrays:
    str2int = { 'START': 0, 'LEADER': 1, 'ADAPTER': 2, 'POLYA': 3, 'TRANSCRIPT': 5 }
    with open(tsv_path, 'r') as f:
        headers =  ['tag','read_id', 'chr', 'idx', 'sample', 'scaled_sample',
                    's_llh', 'l_llh','a_llh','p_llh', 'c_llh', 't_llh','region']
        rdr = csv.DictReader(f, delimiter='\t', quoting=csv.QUOTE_NONE, fieldnames=headers)
        for row in rdr:
            # parse row fields:
            s_llh = float(row['s_llh'])
            l_llh = float(row['l_llh'])
            a_llh = float(row['a_llh'])
            p_llh = float(row['p_llh'])
            t_llh = float(row['t_llh'])
            region = row['region']
            
            # append log-likelihoods to appropriate arrays:
            if region == 'START':
                S_loglkhd.append(s_llh)
            if region == 'LEADER':
                L_loglkhd.append(l_llh)
            if region == 'ADAPTER':
                A_loglkhd.append(a_llh)
            if region == 'POLYA':
                P_loglkhd.append(p_llh)
            if region == 'TRANSCRIPT':
                T_loglkhd.append(t_llh)
    
    return { "S_loglkhd": np.array(S_loglkhd, dtype=float),
             "L_loglkhd": np.array(L_loglkhd, dtype=float),
             "A_loglkhd": np.array(A_loglkhd, dtype=float),
             "P_loglkhd": np.array(P_loglkhd, dtype=float),
             "T_loglkhd": np.array(T_loglkhd, dtype=float) }


def make_segmentation_dict(segmentations_tsv_path):
    """
    Load a segmentations TSV file. Rows of `segmentations_tsv_path` look like this:

    tag                 read_id: pos:       L_0    A_0:    P_0:     P_1:     RR:    P(A)L:  AL:
    polya-segmentation  fc06...  161684804  47.0   1851.0  8354.0   11424.0  73.76  75.18   35.23

    Note that this function only takes the first available segmentation for each read, i.e.
    if a read id appears more than once in the TSV, only the first segmentation is kept, and
    later occurrences of the read id in the TSV are ignored.
    """
    segments = {}
    # loop thru TSV and update the list of segmentations:
    with open(segmentations_tsv_path, 'r') as f:
        headers = ['tag', 'read_id', 'pos', 'L_start', 'A_start', 'P_start', 'P_end', 'rate', 'plen', 'alen']
        rdr = csv.DictReader(f, delimiter='\t', quoting=csv.QUOTE_NONE, fieldnames=headers)
        for row in rdr:
            if row['read_id'] not in segments.keys():
                segments[row['read_id']] = { 'L_start': int(float(row['L_start'])),
                                             'A_start': int(float(row['A_start'])),
                                             'P_start': int(float(row['P_start'])),
                                             'P_end': int(float(row['P_end'])) }
    return segments


def region_search(read_id, sample_ix, segmentations):
    """
    Given a dictionary of ("gold-standard") segmentations, look up the region that a
    given read and sample index belongs to.

    Returns an integer label out of 0,1,2,3,4,5 where:
    0 => START, 1 => LEADER, 2 => ADAPTER, 3 => POLYA, 5 => TRANSCRIPT, 6 => UNKNOWN

    (We skip label '4' because it represents CLIFFs, which we don't track here --- they have
    a uniform distribution.)
    """
    # find read ID in segmentations:
    read_key = None
    for long_read_id in segmentations.keys():
        if long_read_id[0:len(read_id)] == read_id:
            read_key = long_read_id

    # return UNK if read not found:
    if read_key == None:
        return 6

    # find region that `sample_ix` belongs to:
    l_start = segmentations[read_key]['L_start']
    a_start = segmentations[read_key]['A_start']
    p_start = segmentations[read_key]['P_start']
    p_end = segmentations[read_key]['P_end']
    if (sample_ix < l_start):
        return 0
    if (sample_ix < a_start):
        return 1
    if (sample_ix < p_start):
        return 2
    if (sample_ix <= p_end):
        return 3
    if (sample_ix > p_end):
        return 5
    return 6


def new_tsv_to_numpy(tsv_path, segmentations):
    """
    Read a TSV of new, miscalled samples and a dictionary of correct segmentations (coming from
    an older, correct TSV) and return a dict of numpy arrays.

    Args:
    * tsv_path: path to a TSV generated by `nanopolish polya -vv [...]`.
    * segmentations: a dictionary of segmentation intervals, given in numpy format.

    Returns: a dictionary of numpy arrays.
    """
    # instantiate arrays to hold values:
    S_samples = []
    L_samples = []
    A_samples = []
    P_samples = []
    T_samples = []

    # loop over TSV file and append data to arrays:
    with open(tsv_path, 'r') as f:
        headers =  ['tag','read_id', 'chr', 'idx', 'sample', 'scaled_sample',
                    's_llh', 'l_llh','a_llh','p_llh', 'c_llh', 't_llh','region']
        rdr = csv.DictReader(f, delimiter='\t', quoting=csv.QUOTE_NONE, fieldnames=headers)
        for row in rdr:
            scaled_sample = float(row['scaled_sample'])
            read = row['read_id']
            contig = row['chr']
            index = int(row['idx'])
            region = region_search(read, index, segmentations)
            if region == 0:
                S_samples.append(scaled_sample)
            if region == 1:
                L_samples.append(scaled_sample)
            if region == 2:
                A_samples.append(scaled_sample)
            if region == 3:
                P_samples.append(scaled_sample)
            if region == 5:
                T_samples.append(scaled_sample)
            
    return { "S_samples": np.array(S_samples, dtype=float),
             "L_samples": np.array(L_samples, dtype=float),
             "A_samples": np.array(A_samples, dtype=float),
             "P_samples": np.array(P_samples, dtype=float),
             "T_samples": np.array(T_samples, dtype=float) }


def main(old_samples_tsv, old_segmentations_tsv, new_samples_tsv, benchmark=True):
    """
    Infer and print the new values for mu and sigma (for each of S, L, A, P, C, T) to STDOUT.

    Args:
    * old_samples_tsv: path to TSV file containing polya-samples data from an older kmer model.
    * old_segmentations_tsv: path to TSV file containing polya-segmentation data from an older kmer model.
    * new_samples_tsv: path to TSV file containing polya-samples data from the newer kmer model.

    Returns: N/A, prints outputs to STDOUT.
    """
    ### read all samples into numpy arrays:
    print("Loading data from TSV...")
    old_data = old_tsv_to_numpy(old_samples_tsv)
    segmentations = make_segmentation_dict(old_segmentations_tsv)
    new_data = new_tsv_to_numpy(new_samples_tsv, segmentations)
    print("... Datasets loaded.")

    ### infer best possible new mu,sigma for each of S, L, A, P, T:
    print("Fitting gaussians to new scaled samples (this may take a while)...")
    new_mu_S, new_sigma_S = fit_gaussian(new_data['S_samples'])
    new_mu_L, new_sigma_L = fit_gaussian(new_data['L_samples'])
    (new_pi0_A, new_mu0_A, new_sig0_A), (new_pi1_A, new_mu1_A, new_sig1_A) = fit_gmm(new_data['A_samples'], ncomponents=2)
    new_mu_P, new_sigma_P = fit_gaussian(new_data['P_samples'])
    (new_pi0_T, new_mu0_T, new_sig0_T), (new_pi1_T, new_mu1_T, new_sig1_T) = fit_gmm(new_data['T_samples'], ncomponents=2)

    ### print to stdout:
    print("New params for START: mu = {0}, var = {1}, stdv = {2}".format(new_mu_S, new_sigma_S, np.sqrt(new_sigma_S)))
    print("New params for LEADER: mu = {0}, var = {1}, stdv = {2}".format(new_mu_L, new_sigma_L, np.sqrt(new_sigma_L)))
    print("New params for ADAPTER0: pi = {0}, mu = {1}, var = {2}, stdv = {3}".format(new_pi0_A, new_mu0_A, new_sig0_A, np.sqrt(new_sig0_A)))
    print("New params for ADAPTER1: pi = {0}, mu = {1}, var = {2}, stdv = {3}".format(new_pi1_A, new_mu1_A, new_sig1_A, np.sqrt(new_sig1_A)))
    print("New params for POLYA: mu = {0}, var = {1}, stdv = {2}".format(new_mu_P, new_sigma_P, np.sqrt(new_sigma_P)))
    print("New params for TRANSCR0: pi = {0}, mu = {1}, var = {2}, stdv = {3}".format(new_pi0_T, new_mu0_T, new_sig0_T, np.sqrt(new_sig0_T)))
    print("New params for TRANSCR1: pi = {0}, mu = {1}, var = {2}, stdv = {3}".format(new_pi1_T, new_mu1_T, new_sig1_T, np.sqrt(new_sig1_T)))

    ### optionally, benchmark:
    if not benchmark:
        return

    print("===== Emission Log-Likelihood Benchmarks =====")
    old_S_llh = np.mean(old_data['S_loglkhd'])
    new_S_llh = np.mean(norm.logpdf(new_data['S_samples'], loc=new_mu_S, scale=np.sqrt(new_sigma_S)))
    print("> Average START log-probs:")
    print("> Old avg. log-likelihood: {0} | New avg. log-likelihood: {1}".format(old_S_llh, new_S_llh))
    
    old_L_llh = np.mean(old_data['L_loglkhd'])
    new_L_llh = np.mean(norm.logpdf(new_data['L_samples'], loc=new_mu_L, scale=np.sqrt(new_sigma_L)))
    print("> Average LEADER log-probs:")
    print("> Old avg. log-likelihood: {0} | New avg. log-likelihood: {1}".format(old_L_llh, new_L_llh))

    old_A_llh = np.mean(old_data['A_loglkhd'])
    new_A_llh0 = new_pi0_A * norm.pdf(new_data['A_samples'], loc=new_mu0_A, scale=np.sqrt(new_sig0_A))
    new_A_llh1 = new_pi1_A * norm.pdf(new_data['A_samples'], loc=new_mu1_A, scale=np.sqrt(new_sig1_A))
    new_A_llh = np.mean(np.log(new_A_llh0 + new_A_llh1))
    print("> Average ADAPTER log-probs:")
    print("> Old avg. log-likelihood: {0} | New avg. log-likelihood: {1}".format(old_A_llh, new_A_llh))

    old_P_llh = np.mean(old_data['P_loglkhd'])
    new_P_llh = np.mean(norm.logpdf(new_data['P_samples'], loc=new_mu_P, scale=np.sqrt(new_sigma_P)))
    print("> Average POLYA log-probs:")
    print("> Old avg. log-likelihood: {0} | New avg. log-likelihood: {1}".format(old_P_llh, new_P_llh))

    old_T_llh = np.mean(old_data['T_loglkhd'])
    new_T_llh0 = new_pi0_T * norm.pdf(new_data['T_samples'], loc=new_mu0_T, scale=np.sqrt(new_sig0_T))
    new_T_llh1 = new_pi1_T * norm.pdf(new_data['T_samples'], loc=new_mu1_T, scale=np.sqrt(new_sig1_T))
    new_T_llh = np.mean(np.log(new_T_llh0 + new_T_llh1))
    print("> Average TRANSCRIPT log-probs:")
    print("> Old avg. log-likelihood: {0} | New avg. log-likelihood: {1}".format(old_T_llh, new_T_llh))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Infer new Poly(A) emission parameters.")
    parser.add_argument("old_samples_tsv", help="Path to TSV file of old samples.")
    parser.add_argument("segmentation_tsv", help="Path to segmentations for reads.")
    parser.add_argument("new_samples_tsv", help="Path to TSV file of new samples.")
    parser.add_argument("--benchmark", default=True, type=bool, dest="benchmark",
                        help="If `--benchmark=False`, don't the new estimated HMM parameters.")
    args = parser.parse_args()
    # sanity checks:
    assert os.path.exists(args.old_samples_tsv)
    assert os.path.exists(args.segmentation_tsv)
    assert os.path.exists(args.new_samples_tsv)
    # run inference and (optional) benchmarking of new parameters:
    main(args.old_samples_tsv, args.segmentation_tsv, args.new_samples_tsv, benchmark=args.benchmark)