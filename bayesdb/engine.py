#
#   Copyright (c) 2010-2013, MIT Probabilistic Computing Project
#
#   Lead Developers: Jay Baxter and Dan Lovell
#   Authors: Jay Baxter, Dan Lovell, Baxter Eaves, Vikash Mansinghka
#   Research Leads: Vikash Mansinghka, Patrick Shafto
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import time
import inspect
import os
import json
import datetime
import re
import operator
import copy
import math
import ast
#
import pylab
import numpy
import matplotlib.cm
from scipy.stats import pearsonr
from collections import defaultdict
#
import crosscat.utils.api_utils as au
import crosscat.utils.data_utils as du
import bayesdb.settings as S

from crosscat.CrossCatClient import get_CrossCatClient
from _file_persistence_layer import FilePersistenceLayer
from _postgres_persistence_layer import PostgresPersistenceLayer
import utils
import select_utils

class Engine(object):
  def __init__(self, crosscat_engine_type='local', **kwargs):
    self.backend = get_CrossCatClient(crosscat_engine_type, **kwargs)
    self.persistence_layer = FilePersistenceLayer()
    #self.persistence_layer = PostgresPersistenceLayer()

  def start_from_scratch(self):
    self.persistence_layer.start_from_scratch()
    return 'Started db from scratch.'

  def drop_btable(self, tablename):
    """Delete table by tablename."""
    return self.persistence_layer.drop_btable(tablename)

  def list_btables(self):
    """Return names of all btables."""
    return self.persistence_layer.list_btables()

  def delete_model(self, tablename, model_index):
     """Delete one model."""
     return self.persistence_layer.delete_model(tablename)

  def update_datatypes(self, tablename, mappings):
    """
    mappings is a dict of column name to 'continuous', 'multinomial',
    or an int, which signifies multinomial of a specific type.
    TODO: FIX HACKS. Current works by reloading all the data from csv,
    and it ignores multinomials' specific number of outcomes.
    Also, disastrous things may happen if you update a schema after creating models.
    """
    max_modelid = self.persistence_layer.get_max_model_id(tablename)
    if max_modelid is not None:
      return 'Error: cannot update datatypes after models have already been created. Please create a new table.'
    
    # First, get existing cctypes, and T, M_c, and M_r.    
    cctypes = self.persistence_layer.get_cctypes(tablename)
    m_c, m_r, t = self.persistence_layer.get_metadata_and_table(tablename)
    
    # Now, update cctypes, T, M_c, and M_r
    for col, mapping in mappings.items():
      ## TODO: fix this hack! See method's docstring.
      if type(mapping) == int:
        mapping = 'multinomial'
      cctypes[m_c['name_to_idx'][col]] = mapping
    t, m_r, m_c, header = du.read_data_objects(csv_abs_path, cctypes=cctypes)

    # Now, put cctypes, T, M_c, and M_r back into the DB
    self.persistence_layer.update_cctypes(tablename, cctypes)
    self.persistence_layer.update_metadata_and_table(tablename, M_r, M_c, T)

    colnames = [m_c['idx_to_name'][str(idx)] for idx in range(len(m_c['idx_to_name']))]
    return dict(columns=colnames, data=[cctypes], message='Updated schema:\n')

  def _guess_schema(self, header, values, crosscat_column_types, colnames):
    """Guess the schema. Complete the given crosscat_column_types, which may have missing data, into cctypes
    Also make the corresponding postgres column types."""
    postgres_coltypes = []
    cctypes = []
    column_data_lookup = dict(zip(header, numpy.array(values).T))
    have_column_tpes = type(crosscat_column_types) == dict
    for colname in colnames:
      if have_column_tpes and colname in crosscat_column_types:
        cctype = crosscat_column_types[colname]
      else:
        column_data = column_data_lookup[colname]
        cctype = du.guess_column_type(column_data)
        # cctype = 'continuous'
      cctypes.append(cctype)
      if cctype == 'ignore':
        postgres_coltypes.append('varchar(1000)')
      elif cctype == 'continuous':
        postgres_coltypes.append('float8')
      elif cctype == 'multinomial':
        postgres_coltypes.append('varchar(1000)')
    return postgres_coltypes, cctypes
        
  def create_btable(self, tablename, csv, crosscat_column_types):
    """Uplooad a csv table to the predictive db.
    Crosscat_column_types must be a dictionary mapping column names
    to either 'ignore', 'continuous', or 'multinomial'. Not every
    column name must be present in the dictionary: default is continuous."""
    ## First, test if table with this name already exists, and fail if it does
    if self.persistence_layer.check_if_table_exists(tablename):
      raise Exception('Error: btable with that name already exists.')
    
    csv_abs_path = self.persistence_layer.write_csv(tablename, csv)

    ## Parse column names to create table
    csv = csv.replace('\r', '')
    colnames = csv.split('\n')[0].split(',')

    ## Guess schema and create table
    header, values = du.read_csv(csv_abs_path, has_header=True)
    postgres_coltypes, cctypes = self._guess_schema(header, values, crosscat_column_types, colnames)
    self.persistence_layer.create_btable_from_csv(tablename, csv_abs_path, csv, cctypes, postgres_coltypes, colnames)

    return dict(columns=colnames, data=[cctypes], message='Created btable %s. Inferred schema:' % tablename)

  def export_models(self, tablename):
    """Opposite of import models! Save a pickled version of X_L_list, X_D_list, M_c, and T."""
    X_L_list, X_D_list, M_c = self.persistence_layer.get_latent_states(tablename)
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)
    return dict(M_c=M_c, M_r=M_r, T=T, X_L_list=X_L_list, X_D_list=X_D_list)

  def import_models(self, tablename, X_L_list, X_D_list, M_c, T, iterations=0):
    """Import these models as if they are new models"""
    result = self.persistence_layer.add_models(tablename, X_L_list, X_D_list, iterations)
    return dict(message="Successfully imported %d models." % len(X_L_list))
    
  def create_models(self, tablename, n_models):
    """Call initialize n_models times."""
    # Get t, m_c, and m_r, and tableid
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)
    max_modelid = self.persistence_layer.get_max_model_id(tablename)

    # Call initialize on backend
    states_by_model = list()
    for model_index in range(max_modelid, n_models + max_modelid):
      x_l_prime, x_d_prime = self.backend.initialize(M_c, M_r, T)
      states_by_model.append((x_l_prime, x_d_prime))

    # Insert results into persistence layer
    self.persistence_layer.create_models(tablename, states_by_model)

  def analyze(self, tablename, model_index='all', iterations=2, wait=False):
    """Run analyze for the selected table. model_index may be 'all'."""
    # Get M_c, T, X_L, and X_D from database
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)
    
    if (str(model_index).upper() == 'ALL'):
      modelids = self.persistence_layer.get_model_ids(tablename)
      print('modelids: %s' % modelids)
    else:
      modelids = [model_index]

    modelid_iteration_info = list()
    for modelid in modelids:
      iters = self._analyze_helper(tablename, M_c, T, modelid, iterations)
      modelid_iteration_info.append('Model %d: %d iterations' % (modelid, iters))
    return dict(message=', '.join(modelid_iteration_info))

  def _analyze_helper(self, tablename, M_c, T, modelid, iterations):
    """Only for one model."""
    X_L_prime, X_D_prime, prev_iterations = self.persistence_layer.get_model(tablename, modelid)
    X_L_prime, X_D_prime = self.backend.analyze(M_c, T, X_L, X_D, n_steps=iterations)
    self.persistence_layer.add_samples_for_model(tablename, X_L_prime, X_D_prime, prev_iterations + iterations, modelid)
    return (prev_iterations + iterations)

  def infer(self, tablename, columnstring, newtablename, confidence, whereclause, limit, numsamples, order_by=False):
    """Impute missing values.
    Sample INFER: INFER columnstring FROM tablename WHERE whereclause WITH confidence LIMIT limit;
    Sample INFER INTO: INFER columnstring FROM tablename WHERE whereclause WITH confidence INTO newtablename LIMIT limit;
    Argument newtablename == null/emptystring if we don't want to do INTO
    """
    # TODO: actually impute only missing values, instead of all values.
    X_L_list, X_D_list, M_c = self.persistence_layer.get_latent_states(tablename)
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)
    numrows = len(T)

    t_array = numpy.array(T, dtype=float)
    name_to_idx = M_c['name_to_idx']

    if '*' in columnstring:
      col_indices = name_to_idx.values()
    else:
      colnames = [colname.strip() for colname in columnstring.split(',')]
      col_indices = [name_to_idx[colname] for colname in colnames]
      
    Q = []
    for row_idx in range(numrows):
      for col_idx in col_indices:
        if numpy.isnan(t_array[row_idx, col_idx]):
          Q.append([row_idx, col_idx])

    # FIXME: the purpose of the whereclause is to specify 'given'
    #        p(missing_value | X_L, X_D, whereclause)
    ## TODO: should all observed values besides the ones being imputed be givens?
    if whereclause=="" or '=' not in whereclause:
      Y = None
    else:
      varlist = [[c.strip() for c in b.split('=')] for b in whereclause.split('AND')]
      Y = [(numrows+1, name_to_idx[colname], colval) for colname, colval in varlist]
      Y = [(r, c, du.convert_value_to_code(M_c, c, colval)) for r,c,colval in Y]

    counter = 0
    ret = []
    for q in Q:
      out = self.backend.impute_and_confidence(M_c, X_L_list, X_D_list, Y, [q], numsamples)
      value, conf = out
      if conf >= confidence:
        row_idx = q[0]
        col_idx = q[1]
        ret.append((row_idx, col_idx, value))
        counter += 1
        if counter >= limit:
          break
    imputations_list = [(r, c, du.convert_code_to_value(M_c, c, code)) for r,c,code in ret]
    ## Convert into dict with r,c keys
    imputations_dict = defaultdict(dict)
    for r,c,val in imputations_list:
      imputations_dict[r][c] = val
    ret = self.select(tablename, columnstring, whereclause, limit, order_by=order_by, imputations_dict=imputations_dict)
    return ret

  def select(self, tablename, columnstring, whereclause, limit, order_by, imputations_dict=None):
    """
    Our own homebrewed select query.
    First, reads codes from T and converts them to values.
    Then, filters the values based on the where clause.
    Then, fills in all imputed values, if applicable.
    Then, orders by the given order_by functions.
    Then, computes the queried values requested by the column string.

    One refactoring option: you could try generating a list of all functions that will be needed, either
    for selecting or for ordering. Then compute those and add them to the data tuples. Then just do the
    order by as if you're doing it exclusively on columns. The only downside is that now if there isn't an
    order by, but there is a limit, then we computed a large number of extra functions.
    """
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)
    X_L_list, X_D_list, M_c = self.persistence_layer.get_latent_states(tablename)
    
    queries, query_colnames, aggregates_only = select_utils.get_queries_from_columnstring(columnstring, M_c, T)
    where_conditions = select_utils.get_conditions_from_whereclause(whereclause)      

    filtered_rows = select_utils.filter_and_impute_rows(T, M_c, imputations_dict, where_conditions)

    ## TODO: In order to avoid double-calling functions when we both select them and order by them,
    ## we should augment filtered_rows here with all functions that are going to be selected
    ## (and maybe temporarily augmented with all functions that will be ordered only)
    ## If only being selected: then want to compute after ordering...
    
    filtered_rows = select_utils.order_rows(filtered_rows, order_by, M_c, X_L_list, X_D_list, T, self.backend)

    data = select_utils.compute_result_and_limit(filtered_rows, limit, queries, M_c, X_L_list, X_D_list, self.backend)

    return dict(message='', data=data, columns=query_colnames)

  def simulate(self, tablename, columnstring, newtablename, whereclause, numpredictions, order_by):
    """Simple predictive samples. Returns one row per prediction, with all the given and predicted variables."""
    X_L_list, X_D_list, M_c = self.persistence_layer.get_latent_states(tablename)
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)

    numrows = len(M_r['idx_to_name'])
    name_to_idx = M_c['name_to_idx']

    # parse whereclause
    where_col_idxs_to_vals = dict()
    if whereclause=="" or '=' not in whereclause:
      Y = None
    else:
      varlist = [[c.strip() for c in b.split('=')] for b in whereclause.split('AND')]
      Y = []
      for colname, colval in varlist:
        if type(colval) == str or type(colval) == unicode:
          colval = ast.literal_eval(colval)
        where_col_idxs_to_vals[name_to_idx[colname]] = colval
        Y.append((numrows+1, name_to_idx[colname], colval))

      # map values to codes
      Y = [(r, c, du.convert_value_to_code(M_c, c, colval)) for r,c,colval in Y]

    ## Parse queried columns.
    colnames = [colname.strip() for colname in columnstring.split(',')]
    col_indices = [name_to_idx[colname] for colname in colnames]
    query_col_indices = [idx for idx in col_indices if idx not in where_col_idxs_to_vals.keys()]
    Q = [(numrows+1, col_idx) for col_idx in query_col_indices]

    out = self.backend.simple_predictive_sample(M_c, X_L_list, X_D_list, Y, Q, numpredictions)

    # convert to data, columns dict output format
    # map codes to original values
    data = []
    for vals in out:
      row = []
      i = 0
      for idx in col_indices:
        if idx in where_col_idxs_to_vals:
          row.append(where_col_idxs_to_vals[idx])
        else:
          row.append(du.convert_code_to_value(M_c, idx, vals[i]))
          i += 1
      data.append(row)
    ret = {'message': 'Simulated data:', 'columns': colnames, 'data': data}
    return ret

  def estimate_columns(self, tablename, whereclause, limit, order_by, name=None):
    raise NotImplementedError()
  
  def estimate_pairwise(self, tablename, function_name, filename, column_list=None):
    ## TODO: implement functionality with column_list
    if column_list is not None:
      raise NotImplementedError()
    X_L_list, X_D_list, M_c = self.persistence_layer.get_latent_states(tablename)
    M_c, M_r, T = self.persistence_layer.get_metadata_and_table(tablename)
    return self._do_gen_matrix(function_name, X_L_list, X_D_list, M_c, T, tablename, filename)

  def estimate_dependence_probabilities(self, tablename, col, confidence, limit, filename, submatrix):
    X_L_list, X_D_list, M_c = self.persistence_layer.get_latent_states(tablename)
    return self._do_gen_matrix("dependence probability", X_L_list, X_D_list, M_c, tablename, filename, col=col, confidence=confidence, limit=limit, submatrix=submatrix)
  

# helper functions
get_name = lambda x: getattr(x, '__name__')
get_Engine_attr = lambda x: getattr(Engine, x)
is_Engine_method_name = lambda x: inspect.ismethod(get_Engine_attr(x))
#
def get_method_names():
    return filter(is_Engine_method_name, dir(Engine))
#
def get_method_name_to_args():
    method_names = get_method_names()
    method_name_to_args = dict()
    for method_name in method_names:
        method = Engine.__dict__[method_name]
        arg_str_list = inspect.getargspec(method).args[1:]
        method_name_to_args[method_name] = arg_str_list
    return method_name_to_args
