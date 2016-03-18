#from scipy.special import gammaincinv
from scipy.special import gammainc
#from scipy.stats import poisson
from itertools import permutations
from sys import maxsize
import time
from three_bit import ThreeBit as bin_rep       #Using Ambrose coder for unity
import numpy as np
import seqc
import sys

high_value = maxsize  # Used for sorting, needs to be longer than any sequence  

NUM_OF_ERROR_CORRECTION_METHODS = 3
ERROR_CORRECTION_AJC = 0
#ERROR_CORRECTION_STEN = 1
ERROR_CORRECTION_BC_FILTERS = 3
#ERROR_CORRECTION_ALLON = 3
ERROR_CORRECTION_LIKELIHOOD_NOT_ERROR = 0
ERROR_CORRECTION_jaitin = 1
ERROR_CORRECTION_JAIT_LIKELIHOOD = 2

    
    

def prepare_for_ec(ra, barcode_files, required_poly_t=1, reverse_complement=True, max_ed=2, err_correction_mat=''):
    ''' Prepare the RA for error correction. Apply filters, estimate error correction and correct the barcodes'''
    res = {}
    tot = 0
    filtered = 0
    bc_filter = 0
    
    N = bin_rep._str2bindict['N']
    
    dynamic_codes_table_c1 = {}
    dynamic_codes_table_c2 = {}
    
    errors = list(bin_rep.ints2int([p[0],p[1]]) for p in permutations(bin_rep.bin_nums, r=2))
    error_table = dict(zip(errors, [0] * len(errors)))
    cor_instance_table = {bin_rep._str2bindict['A']:0,
                        bin_rep._str2bindict['C']:0,
                        bin_rep._str2bindict['G']:0,
                        bin_rep._str2bindict['T']:0}
    
    #Read the barcodes into lists
    correct_barcodes = []
    if reverse_complement:
        for barcode_file in barcode_files:
            with open(barcode_file) as f:
                correct_barcodes.append(set(bin_rep.str2bin(rev_comp(line.strip()))
                                            for line in f.readlines()))
    else:
        for barcode_file in barcode_files:
            with open(barcode_file) as f:
                correct_barcodes.append(set(bin_rep.str2bin(line.strip())
                                            for line in f.readlines()))
    
    for i, v in enumerate(ra.data):
        tot += 1
        #Apply various perliminary filters on the data
        if v['gene'] == 0:
            filtered += 1
            continue
        if v['cell']==0:
            filtered += 1
            continue
        if v['rmt']==0:
            filtered += 1
            continue
        if v['n_poly_t']<=required_poly_t:
            filtered += 1
            continue
        if bin_rep.contains(int(v['cell']), N):
            filtered += 1
            continue
        if bin_rep.contains(int(v['rmt']), N):
            filtered += 1
            continue
            
        gene = v['gene']
        
        #correct and filter barcodes
        c1 = bin_rep.c1_from_codes(int(v['cell']))
        try:
            cor_c1, ed_1, err_l_1 = dynamic_codes_table_c1[c1]
        except KeyError:
            cor_c1, ed_1 = find_correct_barcode(c1, correct_barcodes[0])
            if ed_1 > max_ed:   #No point calculating errors on codes with more errors than allowed
                err_l_1 = None
            else:
                err_l_1 = list_errors(cor_c1, c1)
            dynamic_codes_table_c1[c1] = (cor_c1, ed_1, err_l_1)
            
        c2 = bin_rep.c2_from_codes(int(v['cell']))
        try:
            cor_c2, ed_2, err_l_2 = dynamic_codes_table_c2[c2]
        except KeyError:
            cor_c2, ed_2 = find_correct_barcode(c2, correct_barcodes[1])
            if ed_2 > max_ed:   #No point calculating errors on codes with more errors than allowed
                err_l_2 = None
            else:
                err_l_2 = list_errors(cor_c2, c2)
            dynamic_codes_table_c2[c2] = (cor_c2, ed_2, err_l_2)
        
        # Filter reads with too many errors in their barcodes
        if ed_1+ed_2 > max_ed:
            bc_filter += 1
            if err_correction_mat!='':
                err_correction_mat[i,ERROR_CORRECTION_BC_FILTERS] = 1
            continue
        
        for er in err_l_1 + err_l_2:
            error_table[er] += 1        
        
        #count non error bases
        tmp_c = bin_rep.ints2int([c1,c2])
        tmp_cor = bin_rep.ints2int([cor_c1,cor_c2])
        while tmp_c>0:
            if tmp_c&0b111 == tmp_cor&0b111:
                cor_instance_table[tmp_c&0b111]+=1
            tmp_c >>= 3
            tmp_cor >>= 3
            
        #group according to the correct barcodes and gene
        cell = bin_rep.ints2int([cor_c1,cor_c2])
        rmt = v['rmt']
        try:
            res[gene,cell][rmt].append(i)
        except KeyError:
            try:
                res[gene,cell][rmt] = [i]
            except KeyError:
                res[gene,cell]={}
                res[gene,cell][rmt]=[i]
        

    # convert to error rates    #TODO: This is ambrose code, low priority to test it and /or replace it with mine.
    default_error_rate = 0.02
    err_rate = dict(zip(errors, [0.0] * len(errors)))
    if sum(error_table.values()) == 0:
        print('No errors were detected, using %f uniform error chance.' % (
            default_error_rate))
        err_rate = dict(zip(errors, [default_error_rate] * len(errors)))
    for k, v in error_table.items():
        try:
            err_rate[k] = v / (sum(n for err_type, n in error_table.items()
                               if err_type&0b111000 == k&0b111000) + cor_instance_table[(k&0b111000)>>3])
        except ZeroDivisionError:
            print('Warning: too few reads to estimate error rate for %r '
                  'setting default rate of %f' % (k, default_error_rate))
            err_rate[k] = default_error_rate            
    #print('total reads: ',tot,' no single gene: ',no_single_gene,' cell is zero: ',cell_0, ' rmt is zero: ',rmt_0,' small poly_t: ',small_poly_t)
    print('total reads: ',tot,' filtered: ',filtered, ' corrected: ', bc_filter)
    #print('error_table: ',error_table,' cor_instance_table: ',cor_instance_table)
    return res, err_rate

# Return the hamming distance bewteen two numbers representing a sequence (3 bits per base)
def hamming_dist_bin(c1, c2):
    if (bin_rep.seq_len(c1) != bin_rep.seq_len(c2)):
        return high_value
    d=0
    while c1>0:
        if c1&0b111 != c2&0b111:
            d+=1
        c1>>=3
        c2>>=3
    return d
    
def generate_close_seq(seq):
    ''' Return a list of all sequences that are up to 2 hamm distance from seq'''
    res = []
    l = bin_rep.seq_len(seq)
    
    #genereta all sequences that are dist 1
    for i in range(l):
        mask = 0b111<<(i*3)
        cur_chr = (seq&mask)>>(i*3)
        res += [seq&(~mask)|(new_chr<<(i*3)) for new_chr in bin_rep.bin_nums if new_chr!=cur_chr]
    #genereta all sequences that are dist 2
    for i in range(l):
        mask_i = 0b111<<(i*3)
        chr_i = (seq&mask_i)>>(i*3)
        for j in range(i+1,l):
            mask_j = 0b111<<(j*3)
            chr_j = (seq&mask_j)>>(j*3)
            mask = mask_i|mask_j
            res += [seq&(~mask)|(new_chr_i<<(i*3))|(new_chr_j<<(j*3)) for new_chr_i in bin_rep.bin_nums if new_chr_i!=chr_i for new_chr_j in bin_rep.bin_nums if new_chr_j!=chr_j]
    
    return res 
    
def prob_d_to_r_bin(d_seq, r_seq, err_rate):
    '''Return the probability of d_seq turning into r_seq based on the err_rate table (all binary)'''
    
    if (bin_rep.seq_len(d_seq) != bin_rep.seq_len(r_seq)):
        return 1
    
    p=1.0
    while d_seq>0:
        if d_seq&0b111 != r_seq&0b111:
            p*= err_rate[bin_rep.ints2int([d_seq&0b111, r_seq&0b111])]
        d_seq>>=3
        r_seq>>=3
    return p
    
complement = {'A': 'T', 'C': 'G', 'G': 'C', 'T': 'A'}  # for reverse complement
def rev_comp(s):
    """Return the reverse complement of an ACGT string s"""
    ret = []
    for i in range(len(s)):
        ret.append(complement[(s[-1-i])])
    return ''.join(ret)
            



def list_errors(s1, s2):
    """
    Return the list of nucleotide transformations that turn s1 to s2.
    An error is a six bit int representing a two chr string of type "AG","CT", etc.
    """
    
    # return the actual error
    err_list = []
    while s1 > 0:
        if s1&0b111 != s2&0b111:
            err_list.append(bin_rep.ints2int([s1&0b111, s2&0b111]))
        s1>>=3
        s2>>=3
    return err_list
    
def find_correct_barcode(code, barcodes_list):
    """
    For a given barcode find the closest correct barcode to it from the list (limited to one ED), a string representing the error and the edit distance
    NOTE: for now this function looks for a barcode with ED==1 and does not bother looking for the minimum
    """
    if code in barcodes_list:
        return code, 0
    
    min_ed = high_value
    cor_code = 0
    for bc in barcodes_list:
        hamm_d = hamming_dist_bin(code, bc)
        if hamm_d == 1:
            min_ed = 1
            cor_code = bc           
            break
        if hamm_d < min_ed:
            min_ed = hamm_d
            cor_code = bc
        
    return cor_code, min_ed


#TODO: check this. clean other ec methods, comments and prob_d_to_r. push.
    
def correct_errors(alignments_ra, barcode_files = [], apply_likelihood=True, reverse_complement=True, donor_cutoff=1, alpha=0.05, required_poly_t=1, max_ed=2):
    '''Recieve an RA and return a bool matrix of identified errors according to each method'''
    res_time_cnt = {}
    err_correction_res = np.zeros((len(alignments_ra), NUM_OF_ERROR_CORRECTION_METHODS))
    ra_grouped, error_rate = prepare_for_ec(alignments_ra, barcode_files, required_poly_t, reverse_complement, max_ed, err_correction_mat='')
    grouped_res_dic, error_count = correct_errors_AJC(alignments_ra, ra_grouped, error_rate, err_correction_res, p_value=alpha)
        
    return grouped_res_dic, err_correction_res
    
        
def correct_errors_AJC(ra, ra_grouped, err_rate, err_correction_res, donor_cutoff=1, p_value=0.05, apply_likelihood=True, fname=''):
    """calculate and correct errors in barcode sets"""
    start = time.process_time()
    d = ra_grouped
    grouped_res_dic = {}
    error_count = 0
    
    tot_feats = len(ra_grouped)
    cur_f = 0
    #f=open(fname,'w+')
    #f.write('gene\tcell\trmt\tgroup size\tnum of observations\tactual donors\texpected errors\texpected correct\tp_val not err\tp_val not correct\tjaitin\n')
    N = bin_rep._str2bindict['N']
    for_removal = []
    for feature, cell in d.keys():
        sys.stdout.write('\r' + str(cur_f) + '/' + str(tot_feats) + ' groups processed. ('+str((100*cur_f)/tot_feats)+'%)')
        cur_f += 1
        if feature==0:  
            continue
        
        retained_rmts = []
        retained_reads = 0
        # Used for testing - calculate how many RMT's in the gene/cell group
        #potential_donors = 0
        #num_rmts = len(d[feature, cell].keys())
        #for r_seq in d[feature, cell].keys():
        #    if bin_rep.contains(r_seq, N):
        #        continue
        #    potential_donors += len(d[feature, cell][r_seq])
        #avg_rmt_size = float(potential_donors) / num_rmts
            
        for r_seq in d[feature, cell].keys():
            if bin_rep.contains(r_seq, N):
                continue
            
            gene = feature
            #r_c1 = bin_rep.c1_from_int(r_seq)
            #r_c2 = bin_rep.c2_from_int(r_seq)
            #r_rmt = bin_rep.rmt_from_int(r_seq)
            r_num_occurences = len(d[gene,cell][r_seq])
            r_pos_list = np.hstack(ra.data['position'][d[feature,cell][r_seq]])

            #threshold = gammaincinv(r_num_occurences, alpha)
            
            expected_errors = 0
            #actual_donors = 0
            p_val_not_err=0
#            p_val_not_correct = poisson.cdf(r_num_occurences, avg_rmt_size) #P(x<=r_num_occurences) Note: these are different distributions
            jait=False
            for d_rmt in generate_close_seq(r_seq):
                #d_seq = bin_rep.ints2int([r_c1,r_c2,d_rmt])
                try:
                    d_num_occurences = len(d[gene, cell][d_rmt])
             #       actual_donors += d_num_occurences
                except KeyError:
                    continue
                #if d_num_occurences<=donor_cutoff:
                #    continue

                p_dtr = prob_d_to_r_bin(d_rmt, r_seq, err_rate)
                expected_errors += d_num_occurences * p_dtr
                
                #do Jaitin
                if not jait:
                    d_pos_list = np.hstack(ra.data['position'][d[feature,cell][d_rmt]])
                    if set(r_pos_list).issubset(set(d_pos_list)):
                        err_correction_res[ra_grouped[gene, cell][r_seq],ERROR_CORRECTION_jaitin] = 1
                        jait=True
                        
            p_val_not_err = gammainc(r_num_occurences, expected_errors)     #P(x>=r_num_occurences)
            if p_val_not_err <= p_value:
                err_correction_res[ra_grouped[gene, cell][r_seq],ERROR_CORRECTION_LIKELIHOOD_NOT_ERROR] = 1
            elif jait:
                err_correction_res[ra_grouped[gene, cell][r_seq],ERROR_CORRECTION_JAIT_LIKELIHOOD] = 1
            
            
            if apply_likelihood:
                if (not jait) or p_val_not_err <= p_value:
                    retained_rmts.append(r_seq)
                    retained_reads+=r_num_occurences
            elif not jait:
                retained_rmts.append(r_seq)
                retained_reads+=r_num_occurences
                
        grouped_res_dic[feature, cell] = retained_rmts, retained_reads
            #f.write(gene+'\t'+bin_rep.bin2str(cell)+'\t'+bin_rep.bin2str(r_seq)+'\t'+str(potential_donors)+'\t'+str(r_num_occurences)+'\t'+str(actual_donors)+'\t'+str(expected_errors)+'\t'+str(avg_rmt_size)+'\t'+str(p_val_not_err)+'\t'+str(p_val_not_correct)+'\t'+str(jait)+'\n')
            #if not (1-poisson.cdf(r_num_occurences, expected_errors) < p_value):        #If the number of observations exceeds that expected by error sufficiently, this is not an error. we mark an error if it doesn't happen
            #    for_removal.append((gene, r_seq))
            #    error_count+=r_num_occurences
                    
    #for (gene, r_seq) in for_removal:
    #    err_correction_res[ra_grouped[gene][r_seq],[ERROR_CORRECTION_AJC]] = 1
    
    #print ('\nLikelihood model error_count: ', error_count)
    tot_time=time.process_time()-start
    print('total error_correction runtime: ',tot_time)
    #f.close()
    return grouped_res_dic, error_count
    
def convert_to_matrix(counts_dictionary):

    # Set up entries for sparse matrix
    cols = [k[0] for k in counts_dictionary.keys()]
    rows = [k[1] for k in counts_dictionary.keys()]
    values = np.array(list(counts_dictionary.values()))

    # Map row and cell to integer values for indexing
    unq_row = np.unique(rows)  # these are the ids for the new rows / cols of the array
    unq_col = np.unique(cols)
    row_map = dict(zip(unq_row, np.arange(unq_row.shape[0])))
    col_map = dict(zip(unq_col, np.arange(unq_col.shape[0])))
    row_ind = np.array([row_map[i] for i in rows])
    col_ind = np.array([col_map[i] for i in cols])

    # change dtype, set shape
    values = values.astype(np.uint32)
    shape = (unq_row.shape[0], unq_col.shape[0])

    # return a sparse array
    coo = coo_matrix((values, (row_ind, col_ind)), shape=shape, dtype=np.uint32)
    return coo, unq_row, unq_col