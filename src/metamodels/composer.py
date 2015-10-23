# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2014, MIT Probabilistic Computing Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import itertools
import math
import sqlite3

import numpy as np

from bayeslite.core import *
from bayeslite.exception import BQLError
from bayeslite.sqlite3_util import sqlite3_quote_name as quote
from bayeslite.util import logmeanexp, casefold

from crosscat.utils import sample_utils as su

import bayeslite.metamodel
import bayeslite.bqlfn

import bdbcontrib

composer_schema_1 = [
'''
INSERT INTO bayesdb_metamodel
    (name, version) VALUES ('composer', 1);
''','''
CREATE TABLE bayesdb_composer_cc_id(
    generator_id INTEGER NOT NULL
        REFERENCES bayesdb_generator(id),
    crosscat_generator_id INTEGER NOT NULL
        REFERENCES bayesdb_generator(id),

    PRIMARY KEY(generator_id, crosscat_generator_id),
    CHECK (generator_id != crosscat_generator_id)
);
''','''
CREATE TABLE bayesdb_composer_column_owner(
    generator_id INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    colno INTEGER NOT NULL,
    local BOOLEAN NOT NULL,

    PRIMARY KEY(generator_id, colno, local),
    FOREIGN KEY(generator_id, colno)
        REFERENCES bayesdb_generator_column(generator_id, colno)
);
''','''
CREATE TABLE bayesdb_composer_column_toposort(
    generator_id INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    colno INTEGER NOT NULL,
        -- Cannot express check constraint with subquery in sqlite.
        -- CHECK (EXISTS(SELECT generator_id, colno
        --      FROM bayesdb_composer_column_owner WHERE local = FALSE)),
    position INTEGER NOT NULL,
        CHECK (0 <= position)

    PRIMARY KEY(generator_id, colno),
    FOREIGN KEY(generator_id, colno)
        REFERENCES bayesdb_generator_column(generator_id, colno),
    UNIQUE (generator_id, position)
);
''','''
CREATE TRIGGER bayesdb_composer_column_toposort_check
    BEFORE INSERT ON bayesdb_composer_column_toposort
BEGIN
    SELECT CASE
        WHEN (NOT EXISTS(SELECT generator_id, colno, local
                FROM bayesdb_composer_column_owner
                WHERE generator_id=NEW.generator_id AND colno=NEW.colno AND
                    local = 0))
        THEN RAISE(ABORT, 'Columns in bayesdb_composer_column_toposort
            must be foreign.')
    END;
END;
''','''
CREATE TABLE bayesdb_composer_column_parents(
    generator_id INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    fcolno INTEGER NOT NULL,
    pcolno INTEGER NOT NULL,

    PRIMARY KEY(generator_id, fcolno, pcolno),
    FOREIGN KEY(generator_id, fcolno)
        REFERENCES bayesdb_generator_column(generator_id, colno),
    FOREIGN KEY(generator_id, pcolno)
        REFERENCES bayesdb_generator_column(generator_id, colno),
    CHECK (fcolno != pcolno)
);
''','''
CREATE TABLE bayesdb_composer_column_foreign_predictor(
    generator_id INTEGER NOT NULL REFERENCES bayesdb_generator(id),
    colno INTEGER NOT NULL,
    predictor_name TEXT COLLATE NOCASE NOT NULL,
    predictor_binary BLOB,

    PRIMARY KEY(generator_id, colno),
    FOREIGN KEY(generator_id, colno)
        REFERENCES bayesdb_generator_column(generator_id, colno)
);
''','''
CREATE TRIGGER bayesdb_composer_column_foreign_predictor_check
    BEFORE INSERT ON bayesdb_composer_column_foreign_predictor
BEGIN
    SELECT CASE
        WHEN (NOT EXISTS(SELECT generator_id, colno, local
                FROM bayesdb_composer_column_owner
                WHERE generator_id=NEW.generator_id AND colno=NEW.colno AND
                    local = 0))
        THEN RAISE(ABORT, 'Columns in bayesdb_composer_foreign_predictor
            must be foreign.')
    END;
END;
''']

class Composer(bayeslite.metamodel.IBayesDBMetamodel):
    """A metamodel which composes foreign predictors with CrossCat.
    """

    def __init__(self, n_samples=None):
        # In-memory map of registered foreign predictor builders.
        self.predictor_builder = {}
        self.predictor_cache = {}
        # Default number of samples.
        if n_samples is None:
            self.n_samples = 100
        else:
            assert 0 < n_samples
            self.n_samples = n_samples

    def register_foreign_predictor(self, builder):
        """Register an object which builds a foreign predictor. The `builder`
        must have the methods:

        - create(bdb, table, targets, conditions)
            Returns a new foreign predictor, typically by calling its `train`
            method (see `IBayesDBForeignPredictor`).

        - serialize(predictor)
            Returns the binary represenation of `predictor`.

        - deserialize(binary)
            Returns the foreign predictor from its `binary` representation.

        - name()
            Returns the name of the builder.

        The current design pattern is to include these four methods in the
        class implementing `IBayesDBForeignPreidctor` as @classmethods. For
        example, suppose RandomForest is a class implementing
        `IBayesDBForeignPreidctor`, then registering RandomForest is achieved
        by registering the **class** instance.

        >> from bdbcontrib.foreign.random_forest import RandomForest
        >> composer.register_foreign_predictor(RandomForest)

        Explicitly initializing a foreign predictor instance is not
        necessary. The `composer` will create, train, and serialize
        all foreign predictors declared in the `schema` when the BQL query
        INITIALIZE is called.

        Foreign predictors must be registered each time the database is
        launched.
        """
        # Validate the builder.
        assert hasattr(builder, 'create')
        assert hasattr(builder, 'serialize')
        assert hasattr(builder, 'deserialize')
        assert hasattr(builder, 'name')
        # Check for duplicates.
        if builder.name() in self.predictor_builder:
            raise ValueError('A foreign predictor with name "{}" has already '
                'been registered. Currently registered: {}'.format(
                    builder.name(), self.predictor_builder))
        self.predictor_builder[casefold(builder.name())] = builder

    def name(self):
        return 'composer'

    def register(self, bdb):
        cursor = bdb.sql_execute('''
            SELECT version FROM bayesdb_metamodel WHERE name = ?;
        ''', (self.name(),))
        version = None
        try:
            row = cursor.next()
        except StopIteration:
            version = 0
        else:
            version = row[0]
        assert version is not None
        if version == 0:
            with bdb.savepoint():
                for stmt in composer_schema_1:
                    bdb.sql_execute(stmt)
        return

    def create_generator(self, bdb, table, schema, instantiate):
        # Parse the schema.
        (columns, lcols, fcols, fcol_to_pcols, fcol_to_fpred,
            dependencies) = self.parse(schema)
        # Instantiate **this** generator.
        genid, bdbcolumns = instantiate(columns.items())
        # Create internal crosscat generator. The name will be the same as
        # this generator name, with a _cc suffix.
        SUFFIX = '_cc'
        cc_name = bayeslite.core.bayesdb_generator_name(bdb, genid) + SUFFIX
        # cc_name = 'satcc'
        # Create strings for crosscat schema.
        cc_cols = ','.join(['{} {}'.format(c, columns[c]) for c in lcols])
        cc_dep = []
        for dep in dependencies:
            if dep[0] == True:
                cc_dep.append('DEPENDENT({})'.format(','.join(dep[1])))
            else:
                cc_dep.append('INDEPENDENT({})'.format(','.join(dep[1])))
        cc_dep = ','.join(cc_dep)
        bql = """
            CREATE GENERATOR {} FOR satellites USING crosscat(
                {}, {}
            );
        """.format(cc_name, cc_cols, cc_dep)
        bdb.execute(bql)
        # Convert strings to column numbers.
        fcolno_to_pcolnos = {}
        for f in fcol_to_pcols:
            fcolno = bayesdb_generator_column_number(bdb, genid, f)
            fcolno_to_pcolnos[fcolno] = [bayesdb_generator_column_number(bdb,
                genid, col) for col in fcol_to_pcols[f]]
        with bdb.savepoint():
            # Save internal cc generator id.
            bdb.sql_execute('''
                INSERT INTO bayesdb_composer_cc_id
                    (generator_id, crosscat_generator_id) VALUES (?,?)
            ''', (genid, bayesdb_get_generator(bdb, cc_name),))
            # Save lcols/fcolnos.
            for colno, _, _ in bdbcolumns:
                local = colno not in fcolno_to_pcolnos
                bdb.sql_execute('''
                    INSERT INTO bayesdb_composer_column_owner
                        (generator_id, colno, local) VALUES (?,?,?)
                ''', (genid, colno, int(local),))
            # Save parents of foreign columns.
            for fcolno in fcolno_to_pcolnos:
                for pcolno in fcolno_to_pcolnos[fcolno]:
                    bdb.sql_execute('''
                        INSERT INTO bayesdb_composer_column_parents
                            (generator_id, fcolno, pcolno) VALUES (?,?,?)
                    ''', (genid, fcolno, pcolno,))
            # Save topological order.
            topo = self.topological_sort(fcolno_to_pcolnos)
            position = 0
            for colno, _ in topo:
                bdb.sql_execute('''
                    INSERT INTO bayesdb_composer_column_toposort
                        (generator_id, colno, position) VALUES (?,?,?)
                    ''', (genid, colno, position,))
                position += 1
            # Save predictor names of foreign columns.
            for fcolno in fcolno_to_pcolnos:
                fp_name = fcol_to_fpred[casefold(
                        bayesdb_generator_column_name(bdb,genid, fcolno))]
                bdb.sql_execute('''
                    INSERT INTO bayesdb_composer_column_foreign_predictor
                        (generator_id, colno, predictor_name) VALUES (?,?,?)
                ''', (genid, fcolno, casefold(fp_name)))

    def drop_generator(self, bdb, genid):
        with bdb.savepoint():
            # Clear caches.
            keys = [k for k in self.predictor_cache if k[0] == genid]
            for k in keys:
                del self.predictor_cache[k]
            # Obtain before losing references.
            cc_name = bayesdb_generator_name(bdb, self.cc_id(bdb, genid))
            # Delete tables reverse order of insertion.
            bdb.sql_execute('''
                DELETE FROM bayesdb_composer_column_foreign_predictor
                    WHERE generator_id = ?
            ''', (genid,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_composer_cc_id
                    WHERE generator_id = ?
            ''', (genid,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_composer_column_toposort
                    WHERE generator_id = ?
            ''', (genid,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_composer_column_parents
                    WHERE generator_id = ?
            ''', (genid,))
            bdb.sql_execute('''
                DELETE FROM bayesdb_composer_column_owner
                    WHERE generator_id = ?
            ''', (genid,))
            # Drop internal crosscat.
            bdb.execute('''
                DROP GENERATOR {}
            '''.format(cc_name))

    def initialize_models(self, bdb, genid, modelnos, model_config):
        # Initialize internal crosscat. If k models of composer are instantiated
        # then k internal CC models will be created (1-1 mapping).
        bql = """
            INITIALIZE {} MODELS FOR {};
            """.format(len(modelnos),
                    bayesdb_generator_name(bdb, self.cc_id(bdb, genid)))
        bdb.execute(bql)
        # Initialize the foriegn predictors.
        for fcol in self.fcols(bdb, genid):
            # Convert column numbers to names.
            targets = \
                [(bayesdb_generator_column_name(bdb, genid, fcol),
                    bayesdb_generator_column_stattype(bdb, genid, fcol))]
            conditions = \
                [(bayesdb_generator_column_name(bdb, genid, pcol),
                    bayesdb_generator_column_stattype(bdb, genid, pcol))
                    for pcol in self.pcols(bdb, genid, fcol)]
            # Initialize the foreign predictor.
            table_name = bayesdb_generator_table(bdb, genid)
            predictor_name = self.predictor_name(bdb, genid, fcol)
            builder = self.predictor_builder[predictor_name]
            predictor = builder.create(bdb, table_name, targets, conditions)
            # Store in the database.
            with bdb.savepoint():
                sql = '''
                    UPDATE bayesdb_composer_column_foreign_predictor SET
                        predictor_binary = :predictor_binary
                        WHERE generator_id = :genid AND colno = :colno
                '''
                predictor_binary = builder.serialize(predictor)
                bdb.sql_execute(sql, {
                    'genid': genid,
                    'predictor_binary': sqlite3.Binary(predictor_binary),
                    'colno': fcol
                })

    def drop_models(self, bdb, genid, modelnos=None):
        raise NotImplementedError('Individual models from compser cannot '
            'be dropped.')

    def analyze_models(self, bdb, genid, modelnos=None, iterations=1,
                max_seconds=None, ckpt_iterations=None, ckpt_seconds=None):
        # XXX Composer currently does not perform joint inference.
        # (Need full GPM interface, active research project).
        self.cc(bdb, genid).analyze_models(bdb, self.cc_id(bdb, genid),
            modelnos=modelnos, iterations=iterations, max_seconds=max_seconds,
            ckpt_iterations=ckpt_iterations, ckpt_seconds=ckpt_seconds)
        # Accounting.
        sql = '''
            UPDATE bayesdb_generator_model
                SET iterations = iterations + :iterations
                WHERE generator_id = :generator_id AND modelno = :modelno
        '''
        if modelnos is None:
            n_model = 0
            while bayesdb_generator_has_model(bdb, genid, n_model):
                n_model += 1
            modelnos = range(n_model)
        with bdb.savepoint():
            for modelno in modelnos:
                bdb.sql_execute(sql, {
                    'generator_id': genid,
                    'modelno': modelno,
                    'iterations': iterations,
                })

    def column_dependence_probability(self, bdb, genid, modelno, colno0,
            colno1):
        # XXX Aggregator only.
        if modelno is None:
            n_model = 1
            while bayesdb_generator_has_model(bdb, genid, n_model):
                n_model += 1
            p = sum(self._column_dependence_probability(bdb, genid, m, colno0,
                colno1) for m in xrange(n_model)) / float(n_model)
        else:
            p = self._column_dependence_probability(bdb, genid, modelno, colno0,
                colno1)
        return p

    def _column_dependence_probability(self, bdb, genid, modelno, colno0,
            colno1):
        # XXX Computes the dependence probability for a single model.
        if modelno is None:
            raise ValueError('Invalid modelno argument for '
                'internal _column_dependence_probability. An integer modelno '
                'is required, not None.')
        # Trivial case.
        if colno0 == colno1:
            return 1
        fcols = set(self.fcols(bdb, genid))
        c0_foreign = colno0 in fcols
        c1_foreign = colno1 in fcols
        # Neither col is foreign, delegate to CrossCat.
        # XXX Fails for future implementation of conditional dependence.
        if not (c0_foreign or c1_foreign):
            return self.cc(bdb, genid).column_dependence_probability(bdb,
                self.cc_id(bdb, genid), modelno,
                self.cc_colno(bdb, genid, colno0),
                self.cc_colno(bdb, genid, colno1))
        # (colno0, colno1) form a (target, conditions) pair.
        # WE explicitly modeled them as dependent by assumption.
        # TODO: Strong assumption? What if FP determines it is not
        # dependent on one of its conditions? (ie 0 coeff in regression)
        if colno0 in self.pcols(bdb, genid, colno1) or \
                colno1 in self.pcols(bdb, genid, colno0):
            return 1
        # (colno0, colno1) form a (local, foreign) pair.
        # IF [col0 FP target], [col1 CC], and [all conditions of col0 IND col1]
        #   then [col0 IND col1].
        # XXX Reverse is not true generally (counterxample), but we shall
        # assume an IFF condition. This assumption is not unlike the transitive
        # closure property of independence in crosscat.
        if (c0_foreign and not c1_foreign) or \
                (c1_foreign and not c0_foreign):
            fcol = colno0 if c0_foreign else colno1
            lcol = colno1 if c0_foreign else colno0
            return any(self._column_dependence_probability(bdb, genid, modelno,
                pcol, lcol) for pcol in self.pcols(bdb, genid, fcol))
        # XXX TODO: Determine independence semantics for this case.
        # Both columns are foreign. (Recursively) return 1 if any of their
        # conditions (possibly FPs) have dependencies.
        assert c0_foreign and c1_foreign
        return any(self._column_dependence_probability(bdb, genid, modelno,
                pcol0, pcol1) for pcol0 in self.pcols(bdb, genid, colno0)
                for pcol1 in self.pcols(bdb, genid, colno1))

    def column_mutual_information(self, bdb, genid, modelno, colno0, colno1,
            numsamples=None):
        if numsamples is None:
            numsamples = self.n_samples
        # XXX Aggregator only.
        X = [colno0]
        W = [colno1]
        Z = Y = []
        mi = []
        if modelno is None:
            n_model = 0
            while bayesdb_generator_has_model(bdb, genid, n_model):
                mi.append(self.conditional_mutual_information(bdb, genid,
                    n_model, X, W, Z, Y))
                n_model += 1
            mi = sum(mi)/n_model
        else:
            mi = self.conditional_mutual_information(bdb, genid, modelno, X, W,
                Z, Y)
        return mi

    def conditional_mutual_information(self, bdb, genid, modelno, X, W, Z, Y,
            numsamples=None):
        # WARNING: SUPER EXPERIMENTAL.
        # Computes the conditional mutual information I(X:W|Z,Y=y), defined
        # defined as the expectation E_z~Z{X:W|Z=z,Y=y}.
        # X, W, and Z must each be a list [colno, ..].
        # Y is an evidence list [(colno,val), ..].
        if numsamples is None:
            numsamples = self.n_samples
        # All sets must be disjoint.
        all_cols = X + W + Z + [y[0] for y in Y]
        if len(all_cols) != len(set(all_cols)):
            raise ValueError('Duplicate colnos received in '
                'conditional_mutual_information.\n'
                'X: {}\nW: {}\nZ: {}\nY: {}'.format(X, W, Z, Y))
        # Simulate from joint.
        XWZ_samples = self.simulate(bdb, genid, modelno, Y, X+W+Z,
            numpredictions=numsamples)
        # Simple Monte Carlo
        mi = logpz = logpxwz = logpxz = logpwz = 0
        for s in XWZ_samples:
            Qx = zip(X, s[:len(X)])
            Qw = zip(Z, s[len(X):-len(Z)])
            Qz = zip(W, s[-len(Z):])
            if Z:
                logpz = self._joint_logpdf(bdb, genid, modelno, Qz, Y)
            else:
                logpz = 0
            logpxwz = self._joint_logpdf(bdb, genid, modelno, Qx+Qw+Qz, Y)
            logpxz = self._joint_logpdf(bdb, genid, modelno, Qx+Qz, Y)
            logpwz = self._joint_logpdf(bdb, genid, modelno, Qw+Qz, Y)
            mi += logpz + logpxwz - logpxz - logpwz
        # TODO: linfoot?
        # TODO: If negative, report to user that reliable answer cannot be
        # returned with current `numsamples`.
        # Averaging is in direct space is correct.
        return mi/numsamples

    def column_value_probability(self, bdb, genid, modelno, colno, value,
            constraints):
        # XXX Aggregator only.
        p = []
        if modelno is None:
            n_model = 0
            while bayesdb_generator_has_model(bdb, genid, n_model):
                p.append(self._joint_logpdf(bdb, genid, n_model,
                    [(colno, value)], constraints))
                n_model += 1
            if all(e == float('-inf') for e in p):
                p = float('-inf')
            else:
                p = logmeanexp(p)
        else:
            p = self._joint_logpdf(bdb, genid, modelno, [(colno, value)],
                constraints)
        return np.exp(p)

    def _joint_logpdf(self, bdb, genid, modelno, Q, Y, n_samples=None):
        # XXX Computes the joint probability of query Q given evidence Y
        # for a single model. The function is a likelihood weighted
        # integrator.
        # XXX Determine.
        if n_samples is None:
            n_samples = self.n_samples
        # Validate inputs.
        if modelno is None:
            raise ValueError('Invalid modelno None, integer requried.')
        if len(Q) == 0:
            raise ValueError('Invalid query Q: len(Q) == 0.')
        # Ensure consistency of any duplicates in Q and Y.
        ignore = set()
        for (cq, vq), (cy, vy) in itertools.product(Q, Y):
            if cq == cy:
                if vq == vy:
                    ignore.add(cq)
                else:
                    return -float('inf')
        Q = [q for q in Q if q not in ignore]
        # (Q,Y) marginal joint density.
        _, QY_weights = self._weighted_sample(bdb, genid, modelno, Q+Y,
            n_samples=n_samples)
        # Y marginal density.
        _, Y_weights = self._weighted_sample(bdb, genid, modelno, Y,
            n_samples=n_samples)
        # XXX TODO Keep sampling until logpQY <= logpY
        logpQY = logmeanexp(QY_weights)
        logpY = logmeanexp(Y_weights)
        return logpQY - logpY

    def predict_confidence(self, bdb, genid, modelno, colno, rowid,
            numsamples=None):
        # Predicts a value for the cell [rowid, colno] with a confidence metric.
        # XXX Prefer accuracy over speed for imputation.
        if numsamples is None:
            numsamples = self.n_samples
        # Obtain all values for all other columns.
        colnos = bayesdb_generator_column_numbers(bdb, genid)
        colnames = bayesdb_generator_column_names(bdb, genid)
        table = bayesdb_generator_table(bdb, genid)
        sql = '''
            SELECT {} FROM {} WHERE _rowid_ = ?
        '''.format(','.join(map(quote, colnames)), quote(table))
        cursor = bdb.sql_execute(sql, (rowid,))
        row = None
        try:
            row = cursor.next()
        except StopIteration:
            generator = bayesdb_generator_table(bdb, genid)
            raise BQLError(bdb, 'No such row in table {} '
                'for generator {}: {}.'.format(table, generator, rowid))
        # Account for multiple imputations if imputing parents.
        parent_conf = 1
        # Predicting lcol.
        if colno in self.lcols(bdb, genid):
            # Delegate to CC IFF
            # (lcol has no children OR all its children are None).
            children = [f for f in self.fcols(bdb, genid) if colno in
                    self.pcols(bdb, genid, f)]
            if len(children) == 0 or \
                    all(row[i] is None for i in xrange(len(row)) if i+1
                        in children):
                return self.cc(bdb, genid).predict_confidence(bdb,
                        self.cc_id(bdb, genid), modelno,
                        self.cc_colno(bdb, genid, colno), rowid)
            else:
                # Obtain likelihood weighted samples from posterior.
                Q = [colno]
                Y = [(c,v) for c,v in zip(colnos, row) if c != colno and v
                        is not None]
                samples = self.simulate(bdb, genid, modelno, Y, Q,
                    numpredictions=numsamples)
                samples = [s[0] for s in samples]
        # Predicting fcol.
        else:
            conditions = {c:v for c,v in zip(colnames, row) if
                bayesdb_generator_column_number(bdb, genid, c) in
                self.pcols(bdb, genid, colno)}
            for colname, val in conditions.iteritems():
                # Impute all missing parents.
                if val is None:
                    imp_col = bayesdb_generator_column_number(bdb, genid,
                        colname)
                    imp_val, imp_conf = self.predict_confidence(bdb, genid,
                        modelno, imp_col, rowid, numsamples=numsamples)
                    # XXX If imputing several parents, take the overall
                    # overall conf as min conf. If we define imp_conf as
                    # P[imp_val = correct] then we might choose to multiply
                    # the imp_confs, but we cannot assert that the imp_confs
                    # are independent so multiplying is extremely conservative.
                    parent_conf = min(parent_conf, imp_conf)
                    conditions[colname] = imp_val
            assert all(v is not None for c,v in conditions.iteritems())
            predictor = self.predictor(bdb, genid, colno)
            samples = predictor.simulate(numsamples, conditions)
        # Since foreign predictor does not know how to impute, imputation
        # shall occur here in the composer by simulate/logpdf calls.
        stattype = bayesdb_generator_column_stattype(bdb, genid, colno)
        if stattype == 'categorical':
            # imp_conf is most frequent.
            imp_val =  max(((val, samples.count(val)) for val in set(samples)),
                key=lambda v: v[1])[0]
            if colno in self.fcols(bdb, genid):
                imp_conf = np.exp(predictor.logpdf(imp_val, conditions))
            else:
                imp_conf = sum(np.array(samples)==imp_val) / len(samples)
        elif stattype == 'numerical':
            # XXX The definition of confidence is P[k=1] where
            # k=1 is the number of mixture componets (we need a distribution
            # over GPMM to answer this question). The confidence is instead
            # implemented as \max_i{p_i} where p_i are the weights of a
            # fitted DPGMM.
            imp_val = np.mean(samples)
            imp_conf = su.continuous_imputation_confidence(samples, None, None,
                n_steps=1000)
        else:
            raise ValueError('Unknown stattype "{}" for a foreign predictor '
                'column encountered in predict_confidence.'.format(stattype))
        return imp_val, imp_conf * parent_conf

    def simulate(self, bdb, genid, modelno, constraints, colnos,
            numpredictions=1):
        # Delegate to crosscat if colnos+constraints all lcols.
        all_cols = [c for c,v in constraints] + colnos
        if all(f not in all_cols for f in self.fcols(bdb, genid)):
            Y_cc = [(self.cc_colno(bdb, genid, c), v)
                for c, v in constraints]
            Q_cc = self.cc_colnos(bdb, genid, colnos)
            return self.cc(bdb, genid).simulate(bdb,
                self.cc_id(bdb, genid), modelno, Y_cc, Q_cc,
                numpredictions=numpredictions)
        # Solve inference problem by sampling-importance resampling.
        result = []
        for i in xrange(numpredictions):
            samples, weights = self._weighted_sample(bdb, genid, modelno,
                constraints)
            p = np.exp(np.asarray(weights) - np.max(weights))
            p /= np.sum(p)
            draw = np.nonzero(np.random.multinomial(1,p))[0][0]
            s = [samples[draw].get(col) for col in colnos]
            result.append(s)
        return result

    def row_similarity(self, bdb, genid, modelno, rowid, target_rowid,
            colnos):
        # XXX Delegate to CrossCat always.
        cc_colnos = self.cc_colnos(bdb, genid, colnos)
        return self.cc(bdb, genid).row_similarity(bdb, self.cc_id(bdb, genid),
            modelno, rowid, target_rowid, cc_colnos)

    def _weighted_sample(self, bdb, genid, modelno, Y, n_samples=None):
        # Returns a pairs of parallel lists ([sample ...], [weight ...])
        # Each `sample` is a vector s=(X1,...,Xn) of values for all nodes in
        # the network. Y specifies evidence nodes: all returned samples have
        # constrained values at the evidence nodes.
        # `weight` is the likelihood of the evidence Y under s\Y.
        if n_samples is None:
            n_samples = self.n_samples
        # Create n_samples dicts, each entry is weighted sample from joint.
        samples = [{c:v for (c,v) in Y} for _ in xrange(n_samples)]
        weights = []
        w0 = 0
        # Assess likelihood of evidence at root.
        Y_cc = [(c, v) for c,v in Y if c in self.lcols(bdb, genid)]
        if Y_cc:
            w0 += self._joint_logpdf_cc(bdb, genid, modelno, Y_cc, [])
        # Simulate latent ccs.
        Q_cc = [c for c in self.lcols(bdb, genid) if c not in samples[0]]
        V_cc = self.cc(bdb, genid).simulate(bdb, self.cc_id(bdb, genid),
            modelno, Y_cc, Q_cc, numpredictions=n_samples)
        for k in xrange(n_samples):
            w = w0
            # Add simulated Q_cc.
            samples[k].update({c:v for c,v in zip(Q_cc, V_cc[k])})
            for fcol in self.topo(bdb, genid):
                pcols = self.pcols(bdb, genid, fcol)
                predictor = self.predictor(bdb, genid, fcol)
                # All parents of FP known (evidence or simulated)?
                assert pcols.issubset(set(samples[k]))
                conditions = {bayesdb_generator_column_name(bdb, genid, c):v
                        for c,v in samples[k].iteritems() if c in pcols}
                # f is evidence: compute likelihood weight.
                if fcol in samples[k]:
                    w += predictor.logpdf(samples[k][fcol], conditions)
                # f is latent: simulate from conditional distribution.
                else:
                    samples[k][fcol] = predictor.simulate(1, conditions)[0]
            weights.append(w)
        return samples, weights

    def _joint_logpdf_cc(self, bdb, genid, modelno, Q, Y):
        # Evaluates the joint logpdf of crosscat columns. This is acheived by
        # invoking column_value_probability on univariate columns with
        # cascading the constraints (the chain rule). This function really
        # belongs somewhere in the famous sample_utils module in crosscat.
        # Ensure consistency for nodes in both query and evidence.
        lcols = self.lcols(bdb, genid)
        ignore = set()
        for (cq, vq), (cy, vy) in itertools.product(Q, Y):
            if cq not in lcols or cy not in lcols:
                raise ValueError('Foreign colno encountered in internal '
                    '_joint_logpdf_cc.')
            if cq == cy:
                if vq == vy:
                    ignore.add(cq)
                else:
                    return -float('inf')
        # Convert.
        Q_cc = []
        for (col, val) in Q:
            if col not in ignore:
                Q_cc.append((self.cc_colno(bdb, genid, col), val))
        Y_cc = []
        for (col, val) in Y:
            Y_cc.append((self.cc_colno(bdb, genid, col), val))
        # Chain rule.
        prob = 0
        for (col, val) in Q_cc:
            r = self.cc(bdb, genid).column_value_probability(bdb,
                    self.cc_id(bdb, genid), modelno, col, val, Y_cc)
            if r == 0:
                return -float('inf')
            prob += math.log(r)
            Y_cc.append((col,val))
        return prob

    def cc_colno(self, bdb, genid, colno):
        return self.cc_colnos(bdb, genid, [colno])[0]

    def cc_colnos(self, bdb, genid, colnos):
        lcolnames = [bayeslite.core.bayesdb_generator_column_name(bdb,
            genid, colno) for colno in colnos]
        return [bayeslite.core.bayesdb_generator_column_number(bdb,
            self.cc_id(bdb, genid), lcolname) for lcolname in lcolnames]

    def cc_id(self, bdb, genid):
        cursor = bdb.sql_execute('''
            SELECT crosscat_generator_id FROM bayesdb_composer_cc_id
                WHERE generator_id = ?
        ''', (genid,))
        return cursor.fetchall()[0][0]

    def cc(self, bdb, genid):
        return bayesdb_generator_metamodel(bdb, self.cc_id(bdb, genid))

    def lcols(self, bdb, genid):
        cursor = bdb.sql_execute('''
            SELECT colno FROM bayesdb_composer_column_owner
                WHERE generator_id = ? AND local = 1
                ORDER BY colno ASC
        ''', (genid,))
        return set([row[0] for row in cursor])

    def fcols(self, bdb, genid):
        cursor = bdb.sql_execute('''
            SELECT colno FROM bayesdb_composer_column_owner
                WHERE generator_id = ? AND local = 0
                ORDER BY colno ASC
        ''', (genid,))
        return set([row[0] for row in cursor])

    def pcols(self, bdb, genid, fcolno):
        cursor = bdb.sql_execute('''
            SELECT pcolno FROM bayesdb_composer_column_parents
                WHERE generator_id = ? AND fcolno = ?
                ORDER BY pcolno ASC
        ''', (genid, fcolno))
        return set([row[0] for row in cursor])

    def topo(self, bdb, genid):
        cursor = bdb.sql_execute('''
            SELECT colno FROM bayesdb_composer_column_toposort
                WHERE generator_id = ?
                ORDER BY position ASC
            ''', (genid,))
        return [row[0] for row in cursor]

    def predictor_name(self, bdb, genid, fcol):
        cursor = bdb.sql_execute('''
            SELECT predictor_name FROM bayesdb_composer_column_foreign_predictor
                WHERE generator_id = ? AND colno = ?
        ''', (genid, fcol))
        return cursor.fetchall()[0][0]

    def predictor(self, bdb, genid, fcol):
        if (genid, fcol) not in self.predictor_cache:
            cursor = bdb.sql_execute('''
                SELECT predictor_name, predictor_binary
                    FROM bayesdb_composer_column_foreign_predictor
                    WHERE generator_id = ? AND colno = ?
            ''', (genid, fcol))
            name, binary = cursor.fetchall()[0]
            builder = self.predictor_builder.get(name, None)
            if builder is None:
                raise LookupError('Foreign predictor for column "{}" '
                    'not registered: "{}".'.format(name,
                        bayesdb_generator_column_name(bdb, genid, fcol)))
            self.predictor_cache[(genid, fcol)] = builder.deserialize(binary)
        return self.predictor_cache[(genid, fcol)]

    def parse(self, schema):
        """Parse the given `schema` for a `composer` metamodel. An example
        of a schema is:

        CREATE GENERATOR foo FOR satellites USING composer(
            default (
                Country_of_Operator CATEGORICAL, Operator_Owner
                CATEGORICAL, Users CATEGORICAL, Purpose CATEGORICAL,
                Class_of_orbit CATEGORICAL, Perigee_km NUMERICAL, Apogee_km
                NUMERICAL, Eccentricity NUMERICAL, Launch_Mass_kg NUMERICAL,
                Dry_Mass_kg NUMERICAL, Power_watts NUMERICAL, Date_of_Launch
                NUMERICAL, Anticipated_Lifetime NUMERICAL, Contractor
                CATEGORICAL, Country_of_Contractor CATEGORICAL, Launch_Site
                CATEGORICAL, Launch_Vehicle CATEGORICAL,
                Source_Used_for_Orbital_Data CATEGORICAL,
                longitude_radians_of_geo NUMERICAL, Inclination_radians
                NUMERICAL
            ),
            random_forest (
                Type_of_Orbit CATEGORICAL
                    GIVEN Apogee_km, Perigee_km,
                        Eccentricity, Period_minutes, Launch_Mass_kg,
                        Power_watts, Anticipated_Lifetime, Class_of_orbit
            ),
            keplers_law (
                Period_minutes NUMERICAL
                    GIVEN Perigee_km, Apogee_km
            ),
            multiple_regression (
                Anticipated_Lifetime NUMERICAL
                    GIVEN Dry_Mass_kg, Launch_Mass_kg, Purpose
            ),
            dependent(Launch_Mass_kg, Dry_Mass_kg, Power_watts),
            dependent(Perigee_km, Apogee_km),
            independent(Operator_Owner, Inclination_radians)
        );

        The schema must adhere to the following rules:

        - Default metamodel is identified `default` or `crosscat`. Every
        `colname` must have its `stattype` declared. IGNORE and GUESS(*) are
        forbidden.

        - Foriegn predictors are identified by the `name()` method of the object
         used when `Composer.register_foreign_predictor` was invoked.
        For example:

            >> from bdbcontrib.foreign.random_forest import RandomForest
            >> composer.register_foreign_predictor(random_forest.RandomForest)
            >> RandomForest.name()
            random_forest

        The grammar inside foreign predictor directives is:
            <target> <stattype> GIVEN <condition> [...[condition]]

        - All columns specified in `dependent` and `independent` directives must
        be modeled by the `default` metamodel.

        Parameters
        ----------
        schema : list<list>
            The `schema` as parsed by bayesdb.

        Returns
        -------
        columns : dict(str:str)
            A dict(colname:stattype) mapping every `colname` declared in
            `schema` to its `stattype`.

        lcols : list<str>
            A list of columns modeled by `default` model.

        fcols : list<str>
            A list of columns modeled by foreign predictor.

        fcol_to_pcols : dict(str:list<str>)
            A dict(fcol:conditions) mapping `fcol` to a list of its parent
            columns.

        fcol_to_fpred : dict(str:str)
            A dict(fcol:fpred) mapping `fcol` to the name of its foreign
            predictor. The values in the dictionary are keys in
            `self.predictor_builder`.

        dependencies : list(<tuple(<bool>,<list<str>)>)
            A list of dependency constraints. Each entry in the list is a tuple.
            For example, (True, ['foo', 'bar', 'baz']) means the three
            variables are mutually and pairwise *dependent*.
        """
        # Allowed keywords.
        DIRECTIVES = ['crosscat', 'default', 'dependent', 'independent'] + \
            self.predictor_builder.keys()
        STATTYPES = ['numerical', 'categorical']
        # Data structures to return.
        columns = {}
        lcols = []
        fcols = []
        fcol_to_pcols = dict()
        fcol_to_fpred = dict()
        dependencies = []
        # Parse!
        for block in schema:
            if len(block) == 0:
                continue
            directive = casefold(block[0])
            commands = block[1]
            if directive not in DIRECTIVES:
                raise ValueError('Unknown directive "{}".\n'
                    'Available directives: {}.'.format(directive, DIRECTIVES))
            if not isinstance(commands, list):
                raise ValueError('Unknown commands in "{}" directive: {}.'\
                        .format(directive, commands))
            if directive == 'default' or directive == 'crosscat':
                while commands:
                    c = casefold(commands.pop(0))
                    if c == ',':
                        continue
                    s = casefold(commands.pop(0))
                    if s not in STATTYPES:
                        raise ValueError('Invalid stattype "{}".'.format(s))
                    columns[c] = s
                    lcols.append(c)
            elif directive == 'independent':
                ind = []
                while commands:
                    c = casefold(commands.pop(0))
                    if c == ',':
                        continue
                    ind.append(c)
                dependencies.append((False, ind))
            elif directive == 'dependent':
                dep = []
                while commands:
                    c = casefold(commands.pop(0))
                    if c == ',':
                        continue
                    dep.append(c)
                dependencies.append((True, dep))
            elif directive in self.predictor_builder:
                c = casefold(commands.pop(0))
                s = casefold(commands.pop(0))
                if s not in STATTYPES:
                    raise ValueError('Invalid stattype "{}".'.format(s))
                columns[c] = s
                given = casefold(commands.pop(0))
                if given != 'given':
                    raise ValueError('Execpted GIVEN keyword, received: {}.'\
                            .format(given))
                conditions = []
                while commands:
                    r = casefold(commands.pop(0))
                    if r == ',':
                        continue
                    conditions.append(r)
                fcols.append(c)
                fcol_to_pcols[c] = conditions
                fcol_to_fpred[c] = directive
        # Unique lcols.
        if len(lcols) != len(set(lcols)):
            raise ValueError('Duplicate default columns enountered: {}.'\
                .format(lcols))
        # Unique fcols.
        if len(fcols) != len(set(fcols)):
            raise ValueError('Duplicate foreign columns enountered: {}.'\
                .format(fcols))
        # All stattypes declared.
        for f, c in fcol_to_pcols.iteritems():
            for r in c:
                if r not in columns:
                    raise ValueError('No stattype declaration for "{}".'\
                        .format(r))
        # No col both lcol and fcol.
        for l in lcols:
            if l in fcol_to_pcols:
                raise ValueError('Column "{}" can only be modeled once.'\
                    .format(l))
        # No non-default dependencies.
        for dep in dependencies:
            for col in dep[1]:
                if col not in lcols:
                    raise ValueError('Column "{}" with dependency constraint '
                        'must have default model.'.format(col))
        # Return the hodgepodge.
        return (columns, lcols, fcols, fcol_to_pcols, fcol_to_fpred,
            dependencies)

    @staticmethod
    def topological_sort(graph):
        """Topologically sort a directed graph represented as an adjacency list.
        Assumes that edges are incoming, ie (10:[8,7]) means 8->10 and 7->10.

        Parameters
        ----------
        graph : list or dict
            Adjacency list or dict representing the graph, for example:
                graph_l = [(10, [8, 7]), (5, [8, 7, 9, 10, 11, 13, 15])]
                graph_d = {10: [8, 7], 5: [8, 7, 9, 10, 11, 13, 15]}

        Returns
        -------
        graph_sorted : list
            An adjacency list, where the order of the nodes is listed
            in topological order.
        """
        graph_sorted = []
        graph = dict(graph)
        # Run until the unsorted graph is empty.
        while graph:
            acyclic = False
            for node, edges in graph.items():
                for edge in edges:
                    if edge in graph:
                        break
                else:
                    acyclic = True
                    del graph[node]
                    graph_sorted.append((node, edges))
            if not acyclic:
                raise ValueError('A cyclic dependency occurred in '
                    'topological_sort.')
        return graph_sorted
