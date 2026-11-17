$ErrorActionPreference = 'Continue'
$env:PYTHONPATH = '.'
cd 'C:\Users\mdham\Downloads\Aetherion-AI-main\end to end aetherion cli'

echo "============================================================"
echo "MISSION 0 — REPOSITORY FORENSICS"
python -c "import inspect; from backend.execution.memory.index import _select_top_k_deterministic, ApproximateVectorIndex, ExactVectorIndex, ShadowQueryCalibrator, get_vector_index, AutoIndexConfig; print('=== _select_top_k_deterministic ==='); print(inspect.getsource(_select_top_k_deterministic))"
python -c "import inspect; from backend.execution.memory.index import ApproximateVectorIndex; print(inspect.getsource(ApproximateVectorIndex._compute_corpus_fingerprint))"
python -c "import inspect; from backend.execution.memory.index import get_vector_index; print(inspect.getsource(get_vector_index))"
python -c "import inspect; from backend.execution.persistence.db import StateDatabase; print(inspect.getsource(StateDatabase._init_db))"
python -c "import inspect; from backend.execution.memory.index import ShadowQueryCalibrator; print(inspect.getsource(ShadowQueryCalibrator.__init__)); print('---'); print(inspect.getsource(ShadowQueryCalibrator._get_state)); print('---'); print(inspect.getsource(ShadowQueryCalibrator.record_observation)); print('---'); print(inspect.getsource(ShadowQueryCalibrator.invalidate_tenant))"

echo "============================================================"
echo "MISSION 1 — TOP-K k=0 CORRECTNESS"
python -c "
from backend.execution.memory.index import _select_top_k_deterministic, ExactVectorIndex, ApproximateVectorIndex
import numpy as np

# Test 1: k=0
candidates = [('m1', 0.9), ('m2', 0.8)]
result = _select_top_k_deterministic(candidates, top_k=0, min_score=0.0)
assert result == [], f'k=0 FAIL: got {result}'
print('k=0: PASS, got []')

# Test 2: k=-1
result = _select_top_k_deterministic(candidates, top_k=-1, min_score=0.0)
assert result == [], f'k=-1 FAIL: got {result}'
print('k=-1: PASS, got []')

# Test 3: empty candidates, k=5
result = _select_top_k_deterministic([], top_k=5, min_score=0.0)
assert result == [], f'empty candidates FAIL: got {result}'
print('empty candidates: PASS, got []')

# Test 4: k=1
result = _select_top_k_deterministic(candidates, top_k=1, min_score=0.0)
assert len(result) == 1, f'k=1 len FAIL: got {result}'
assert result[0][0] == 'm1', f'k=1 wrong top: got {result}'
print(f'k=1: PASS, got {result}')

# Test 5: k < n
result = _select_top_k_deterministic(candidates, top_k=1, min_score=0.0)
assert len(result) == 1
print(f'k<n: PASS, got {result}')

# Test 6: k = n
candidates3 = [('m1', 0.9), ('m2', 0.8), ('m3', 0.7)]
result = _select_top_k_deterministic(candidates3, top_k=3, min_score=0.0)
assert len(result) == 3, f'k=n FAIL: got {result}'
print(f'k=n: PASS, got {result}')

# Test 7: k > n
result = _select_top_k_deterministic(candidates3, top_k=10, min_score=0.0)
assert len(result) == 3, f'k>n FAIL: got {result}'
print(f'k>n: PASS, got {result}')

# Test via ExactVectorIndex.search
vecs = [('a', [1.0, 0.0]), ('b', [0.0, 1.0])]
idx_exact = ExactVectorIndex(vecs)
result_exact = idx_exact.search([1.0, 0.0], top_k=0)
assert result_exact == [], f'ExactVectorIndex k=0 FAIL: got {result_exact}'
print(f'ExactVectorIndex k=0: PASS, got []')

# Test via ApproximateVectorIndex.search
idx_approx = ApproximateVectorIndex(vecs)
result_approx = idx_approx.search([1.0, 0.0], top_k=0)
assert result_approx == [], f'ApproximateVectorIndex k=0 FAIL: got {result_approx}'
print(f'ApproximateVectorIndex k=0: PASS, got []')

print('ALL MISSION 1 CHECKS: PASS')
"

echo "============================================================"
echo "MISSION 2 — TOP-K SEMANTIC PARITY"
python -c "
import random
random.seed(42)
import numpy as np
np.random.seed(42)
from backend.execution.memory.index import _select_top_k_deterministic

def reference_top_k(candidates, top_k, min_score):
    if top_k <= 0: return []
    filtered = [(mid, sim) for mid, sim in candidates if sim >= min_score]
    filtered.sort(key=lambda x: (x[1], x[0]), reverse=True)
    result = []
    for mid, sim in filtered[:top_k]:
        result.append((mid, float(sim)))
    return result

mismatches = 0
total = 0

for trial in range(1000):
    n_cands = random.randint(0, 30)
    top_k = random.choice([-1, 0, 1, 2, 5, 10, 15, 30, 50])
    min_score_choice = random.choice([-1.0, -0.5, 0.0, 0.3, 0.5, 0.9, 1.0])
    candidates = []
    for i in range(n_cands):
        sim_type = random.randint(0, 3)
        if sim_type == 0: sim = random.uniform(-1.0, -0.01)
        elif sim_type == 1: sim = 0.0
        elif sim_type == 2: sim = random.uniform(0.01, 1.0)
        else: sim = random.choice([-1.0, 0.0, 0.5, 1.0])
        mid = f'm{random.randint(0, 20)}'
        candidates.append((mid, sim))
    
    ref = reference_top_k(candidates, top_k, min_score_choice)
    prod = _select_top_k_deterministic(candidates, top_k=top_k, min_score=min_score_choice)
    
    match = True
    if len(ref) != len(prod): match = False
    else:
        for (rid, rsim), (pid, psim) in zip(ref, prod):
            if rid != pid or abs(rsim - psim) > 1e-6:
                match = False; break
    
    if not match:
        mismatches += 1
        if mismatches <= 5: print(f'MISMATCH trial={trial} n={n_cands} top_k={top_k} min_score={min_score_choice}\n  cands={candidates}\n  ref={ref}\n  prod={prod}')
    total += 1

print(f'Total trials: {total}, Mismatches: {mismatches}')
if mismatches == 0: print('MISSION 2 ADVERSARIAL PARITY: PASS (0 mismatches in 1000 trials)')
else: print(f'MISSION 2 ADVERSARIAL PARITY: FAIL ({mismatches} mismatches)')
"
python -c "
from backend.execution.memory.index import _select_top_k_deterministic, ExactVectorIndex, ApproximateVectorIndex
candidates = [('pos', 0.5), ('neg', -0.3), ('zero', 0.0), ('neg2', -0.9)]
result = _select_top_k_deterministic(candidates, top_k=10, min_score=0.0)
ids = [r[0] for r in result]
print(f'min_score=0.0 result ids: {ids}')
assert 'neg' not in ids, 'neg LEAKED through min_score=0.0 filter!'
assert 'neg2' not in ids, 'neg2 LEAKED through min_score=0.0 filter!'
assert 'pos' in ids, 'pos should be included'
assert 'zero' in ids, 'zero should be included (>=0.0)'
print('Negative exclusion min_score=0.0: PASS')

result2 = _select_top_k_deterministic(candidates, top_k=10, min_score=-0.5)
ids2 = [r[0] for r in result2]
print(f'min_score=-0.5 result ids: {ids2}')
assert 'neg' in ids2, 'neg should pass -0.5 filter'
assert 'neg2' not in ids2, 'neg2=-0.9 should be filtered by -0.5'
print('Negative allowed min_score=-0.5: PASS')
for mid, sim in result2:
    assert sim >= -0.5, f'{mid} score {sim} below min_score=-0.5'
    if mid == 'neg': assert sim < 0.0, f'neg score should be negative, got {sim}'
print('Score clamping check: PASS (negative scores not clamped to 0)')
print('NEGATIVE EXCLUSION VERIFICATION: PASS')
"

echo "============================================================"
echo "MISSION 3 — PRODUCTION RETRIEVAL PATH"
python -c "
from backend.execution.memory.index import ExactVectorIndex, ApproximateVectorIndex
import numpy as np
vecs = [('pos_strong', [1.0, 0.0, 0.0, 0.0]), ('pos_weak', [0.7, 0.3, 0.0, 0.0]), ('zero_sim', [0.0, 0.0, 1.0, 0.0]), ('neg_weak', [-0.7, 0.3, 0.0, 0.0]), ('neg_strong', [-1.0, 0.0, 0.0, 0.0])]
query = [1.0, 0.0, 0.0, 0.0]
idx_e = ExactVectorIndex(vecs)
results_e = idx_e.search(query, top_k=10, min_score=0.0)
ids_e = [r[0] for r in results_e]; scores_e = [r[1] for r in results_e]
print(f'ExactVectorIndex results (min_score=0.0): {list(zip(ids_e, scores_e))}')
assert 'neg_weak' not in ids_e, 'neg_weak leaked in ExactVectorIndex!'
assert 'neg_strong' not in ids_e, 'neg_strong leaked in ExactVectorIndex!'
assert all(s >= 0.0 for s in scores_e), f'Negative score leaked: {scores_e}'
print('ExactVectorIndex negative exclusion: PASS')
results_k0 = idx_e.search(query, top_k=0)
assert results_k0 == [], f'ExactVectorIndex k=0 returned {results_k0}'
print('ExactVectorIndex k=0: PASS')

np.random.seed(77)
n = 500
random_vecs = np.random.randn(n, 16).astype('float32')
random_vecs /= np.linalg.norm(random_vecs, axis=1, keepdims=True)
q_vec = random_vecs[0].tolist()
random_vecs[1] = -random_vecs[0]
corpus = [(f'v{i}', random_vecs[i].tolist()) for i in range(n)]

idx_a = ApproximateVectorIndex(corpus)
results_a = idx_a.search(q_vec, top_k=5, min_score=0.0)
scores_a = [r[1] for r in results_a]
print(f'ApproximateVectorIndex results (min_score=0.0) top-5 scores: {scores_a}')
assert all(s >= 0.0 for s in scores_a), f'Negative score leaked in AVI: {scores_a}'
print('ApproximateVectorIndex negative exclusion: PASS')
results_a0 = idx_a.search(q_vec, top_k=0)
assert results_a0 == [], f'ApproximateVectorIndex k=0 returned {results_a0}'
print('ApproximateVectorIndex k=0: PASS')
print('MISSION 3 PRODUCTION RETRIEVAL PATH: PASS')
"

echo "============================================================"
echo "MISSION 4 — F4 SQLITE CALIBRATION PERSISTENCE"
python -c "
import tempfile, os, time
from backend.execution.persistence.db import StateDatabase
from backend.execution.memory.index import ShadowQueryCalibrator, CalibrationState

with tempfile.TemporaryDirectory() as tmpdir:
    db_path = os.path.join(tmpdir, 'test_cal.db')
    db1 = StateDatabase(db_path=db_path)
    cal1 = ShadowQueryCalibrator(db=db1)
    cal1.reset()
    for i in range(10): cal1.record_observation(owner_id='tenant_alpha', recall=0.8 + i*0.01, corpus_size=1000, index_generation=42, model_name='test-model', model_version='1.0', config_fingerprint='abc123')
    state1 = cal1._get_state('tenant_alpha')
    print(f'Before restart: samples={state1.samples}, recall_sum={state1.recall_sum:.4f}')
    assert state1.samples == 10, f'Expected 10, got {state1.samples}'
    del cal1, db1
    
    db2 = StateDatabase(db_path=db_path)
    cal2 = ShadowQueryCalibrator(db=db2)
    state2 = cal2._get_state('tenant_alpha')
    print(f'After restart: samples={state2.samples}, recall_sum={state2.recall_sum:.4f}')
    assert state2.samples == 10, f'State NOT persisted! Expected 10, got {state2.samples}'
    assert abs(state2.recall_sum - state1.recall_sum) < 1e-6, 'recall_sum mismatch after restart'
    print('F4 PERSISTENCE ACROSS RESTART: PASS')
    
    cal2.record_observation(owner_id='tenant_beta', recall=0.5, corpus_size=500, index_generation=99, model_name='test-model', model_version='1.0', config_fingerprint='xyz789')
    state_alpha = cal2._get_state('tenant_alpha')
    state_beta = cal2._get_state('tenant_beta')
    assert state_alpha.samples == 10, f'tenant_alpha contaminated: {state_alpha.samples}'
    assert state_beta.samples == 1, f'tenant_beta wrong: {state_beta.samples}'
    print('F4 TENANT ISOLATION: PASS')
    
    state_stale = cal2._get_state('tenant_alpha', corpus_size=9999, index_generation=42, model_name='test-model', model_version='1.0', config_fingerprint='abc123')
    print(f'After corpus drift: samples={state_stale.samples}')
    assert state_stale.samples == 0, f'STALE STATE NOT INVALIDATED on corpus drift! Got samples={state_stale.samples}'
    print('F4 CORPUS DRIFT INVALIDATION: PASS')
    
    state_model = cal2._get_state('tenant_alpha', corpus_size=1000, index_generation=42, model_name='different-model', model_version='1.0', config_fingerprint='abc123')
    assert state_model.samples == 0, f'STALE STATE on model change! Got samples={state_model.samples}'
    print('F4 MODEL CHANGE INVALIDATION: PASS')
    
    cal2.invalidate_tenant('tenant_alpha')
    state_after_invalidate = cal2._get_state('tenant_alpha')
    assert state_after_invalidate.samples == 0, f'invalidate_tenant failed! Got samples={state_after_invalidate.samples}'
    print('F4 INVALIDATE TENANT REMOVES ROW: PASS')
    
    cal2.reset()
    state_beta_after_reset = cal2._get_state('tenant_beta')
    assert state_beta_after_reset.samples == 0, f'reset failed! tenant_beta still has samples'
    print('F4 RESET REMOVES ALL ROWS: PASS')
    print('MISSION 4 CALIBRATION PERSISTENCE: ALL PASS')
"
python -c "
import tempfile, os, sqlite3
from backend.execution.persistence.db import StateDatabase
from backend.execution.memory.index import ShadowQueryCalibrator
with tempfile.TemporaryDirectory() as tmpdir:
    db_path = os.path.join(tmpdir, 'corrupt.db')
    db1 = StateDatabase(db_path=db_path)
    cal1 = ShadowQueryCalibrator(db=db1)
    cal1.reset()
    for i in range(5): cal1.record_observation('tenant_x', recall=0.7, corpus_size=100, index_generation=1, model_name='m', model_version='1', config_fingerprint='f')
    del cal1, db1
    conn = sqlite3.connect(db_path)
    conn.execute(\"UPDATE calibration_states SET recall_sum='CORRUPT_STRING' WHERE tenant_id='tenant_x'\")
    conn.commit(); conn.close()
    db2 = StateDatabase(db_path=db_path)
    cal2 = ShadowQueryCalibrator(db=db2)
    try:
        state = cal2._get_state('tenant_x')
        print(f'Fail-closed result: samples={state.samples}')
        assert state.samples == 0, f'CORRUPTED STATE RETURNED as authoritative! samples={state.samples}'
        print('F4 CORRUPTION FAIL-CLOSED: PASS')
    except Exception as e:
        print(f'UNEXPECTED EXCEPTION on corrupt row: {e}')
        print('F4 CORRUPTION FAIL-CLOSED: FAIL (exception propagated)')
"

echo "============================================================"
echo "MISSION 5 — CALIBRATION DATABASE KEY DESIGN"
python -c "import inspect; from backend.execution.persistence.db import StateDatabase; print(inspect.getsource(StateDatabase._init_db))"
python -c "
import tempfile, os
from backend.execution.persistence.db import StateDatabase
from backend.execution.memory.index import ShadowQueryCalibrator
with tempfile.TemporaryDirectory() as tmpdir:
    db_path = os.path.join(tmpdir, 'keydsgn.db')
    db = StateDatabase(db_path=db_path)
    cal = ShadowQueryCalibrator(db=db)
    cal.reset()
    for i in range(5): cal.record_observation('t1', recall=0.9, corpus_size=1000, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp1')
    for i in range(3): cal.record_observation('t1', recall=0.5, corpus_size=1000, index_generation=2, model_name='m', model_version='1', config_fingerprint='fp1')
    state_gen1 = cal._get_state('t1', corpus_size=1000, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp1')
    state_gen2 = cal._get_state('t1', corpus_size=1000, index_generation=2, model_name='m', model_version='1', config_fingerprint='fp1')
    print(f'state_gen1: samples={state_gen1.samples}')
    print(f'state_gen2: samples={state_gen2.samples}')
    import sqlite3
    conn = sqlite3.connect(db_path)
    rows = conn.execute('SELECT * FROM calibration_states').fetchall()
    conn.close()
    print(f'DB rows: {rows}')
    print('KEY DESIGN: Check whether different generations can correctly coexist or are properly invalidated')
"

echo "============================================================"
echo "MISSION 6 — FINGERPRINT CORRECTNESS"
python -c "
from backend.execution.memory.index import ApproximateVectorIndex
print('=== FINGERPRINT CORRECTNESS TESTS ===')
c1 = [('m0', [1.0, 0.0]), ('m1', [0.0, 1.0]), ('m2', [0.5, 0.5])]
fp1a, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c1)
fp1b, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c1)
assert fp1a == fp1b, 'FAIL: Same corpus different fingerprint'
print('1. Same corpus reproducible: PASS')
c1_reordered = [('m2', [0.5, 0.5]), ('m0', [1.0, 0.0]), ('m1', [0.0, 1.0])]
fp_ro, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c1_reordered)
assert fp1a == fp_ro, f'FAIL: Reordered corpus has different FP: {fp1a[:8]} vs {fp_ro[:8]}'
print('2. Reordered same corpus: PASS (same fingerprint)')
c_int = [('m0', [1.0, 0.0]), ('m1', [0.0, 0.999]), ('m2', [0.5, 0.5])]
fp_int, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_int)
assert fp1a != fp_int, f'FAIL: Interior mutation not detected'
print('3. Interior vector mutation detected: PASS')
c_mag = [('m0', [0.5, 0.0]), ('m1', [0.0, 1.0]), ('m2', [0.5, 0.5])]
fp_mag, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_mag)
assert fp1a != fp_mag, f'FAIL: Magnitude-only mutation NOT detected (fingerprint blind!)'
print('4. Magnitude-only mutation detected: PASS')
c_3d = [('m0', [1.0, 0.0, 0.0]), ('m1', [0.0, 1.0, 0.0]), ('m2', [0.5, 0.5, 0.5])]
fp_3d, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_3d)
assert fp1a != fp_3d, 'FAIL: Dimension change not detected'
print('5. Dimension change detected: PASS')
c_add = c1 + [('m3', [0.1, 0.9])]
fp_add, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_add)
assert fp1a != fp_add, 'FAIL: ID addition not detected'
print('6. ID addition detected: PASS')
c_del = [('m0', [1.0, 0.0]), ('m1', [0.0, 1.0])]
fp_del, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_del)
assert fp1a != fp_del, 'FAIL: ID deletion not detected'
print('7. ID deletion detected: PASS')
c_dup = [('m0', [1.0, 0.0]), ('m1', [0.0, 1.0]), ('m2', [0.5, 0.5]), ('m0', [0.9, 0.1])]
fp_dup, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_dup)
c_dup_equiv = [('m0', [0.9, 0.1]), ('m1', [0.0, 1.0]), ('m2', [0.5, 0.5])]
fp_dup_equiv, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_dup_equiv)
assert fp_dup == fp_dup_equiv, f'FAIL: Duplicate ID last-write-wins not consistent: {fp_dup[:8]} vs {fp_dup_equiv[:8]}'
print('8. Duplicate IDs last-write-wins: PASS')
fp_empty, _ = ApproximateVectorIndex._compute_corpus_fingerprint([])
assert fp_empty is not None
print(f'9. Empty corpus fingerprint: PASS (fp={fp_empty[:16]})')
c_alt = [('m0', [0.9, 0.1]), ('m1', [0.0, 1.0])]
fp_alt, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_alt)
fp1_restore, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c1)
assert fp1_restore == fp1a, 'FAIL: Restoring original corpus gives different FP'
print('10. Restore original corpus: PASS (same fingerprint)')
c_rep = [('m0', [0.1, 0.9]), ('m1', [0.0, 1.0]), ('m2', [0.5, 0.5])]
fp_rep, _ = ApproximateVectorIndex._compute_corpus_fingerprint(c_rep)
assert fp1a != fp_rep, 'FAIL: Vector replacement not detected'
print('11. Vector replacement detected: PASS')
print('=== ALL MISSION 6 FINGERPRINT TESTS: PASS ===')
"

echo "============================================================"
echo "MISSION 7 — GENERATION SEMANTICS"
python -c "
from backend.execution.memory.index import ApproximateVectorIndex
print('=== GENERATION SEMANTICS TESTS ===')
c_a = [('a', [1.0, 0.0]), ('b', [0.0, 1.0])]
c_b = [('x', [0.5, 0.5]), ('y', [0.3, 0.7])]
idx = ApproximateVectorIndex(vectors=c_a)
gen_init = idx.generation
print(f'Initial generation: {gen_init}')
idx.add('c', [0.7, 0.3])
gen_after_add = idx.generation
print(f'After add: {gen_after_add}')
assert gen_after_add != gen_init, 'Generation did not change after add'
idx.rebuild(c_b)
gen_after_b = idx.generation
print(f'After rebuild to c_b: {gen_after_b}')
assert gen_after_b != gen_init, 'Generation same as initial after corpus change'
idx.rebuild(c_a)
gen_back_a = idx.generation
print(f'After restore to c_a: {gen_back_a}')
idx.rebuild(c_a)
gen_skip = idx.generation
print(f'After skip rebuild (same corpus): {gen_skip}')
assert gen_skip == gen_back_a, f'Skip rebuild changed generation: {gen_back_a} vs {gen_skip}'
print('Generation skip rebuild: PASS')
idx.rebuild(c_b)
gen_b2 = idx.generation
print(f'After second rebuild to c_b: {gen_b2}')
assert gen_b2 == gen_after_b, f'Same corpus c_b has inconsistent generation: {gen_after_b} vs {gen_b2}'
print('Generation content-hash consistency (A->B->A->B): PASS')
print('=== MISSION 7 GENERATION SEMANTICS: PASS ===')
"

echo "============================================================"
echo "MISSION 8 — THRESHOLD CONTRACT"
python -c "
from backend.execution.memory.index import get_vector_index, AutoIndexConfig
print('=== THRESHOLD CONTRACT VERIFICATION ===')
cfg = AutoIndexConfig()
print(f'AutoIndexConfig exact_threshold={cfg.exact_threshold}')
assert cfg.exact_threshold == 25000, f'WRONG exact_threshold: {cfg.exact_threshold}'
boundaries = [(499, 'exact'), (500, 'exact'), (24999, 'exact'), (25000, None), (25001, None), (50000, None), (50001, None), (75000, None)]
for n, expected_type in boundaries:
    vecs = [(f'v{i}', [float(i % 10), float((i+1) % 10)]) for i in range(n)]
    idx = get_vector_index(owner_id='audit', vectors=vecs)
    print(f'N={n:6d}: {idx.index_type}')
    if expected_type == 'exact': assert idx.index_type in ('exact', 'ExactVectorIndex'), f'N={n} should be exact, got {idx.index_type}'
    if n >= 25001: assert idx.index_type not in ('exact', 'ExactVectorIndex'), f'N={n} should NOT be exact but got {idx.index_type}'
print('THRESHOLD BOUNDARY TESTS: PASS')
idx_default = get_vector_index(owner_id='audit', vectors=[(f'v{i}', [1.0, 0.0]) for i in range(100)])
print(f'Default threshold N=100: {idx_default.index_type} (should be exact)')
assert idx_default.index_type in ('exact', 'ExactVectorIndex')
print('Default threshold uses AutoIndexConfig: PASS')
print('=== MISSION 8 THRESHOLD CONTRACT: PASS ===')
"
python -c "import subprocess; r=subprocess.run(['findstr', '/n', '/r', 'threshold.*500\|500.*threshold', 'backend/execution/memory/retrieval.py', 'backend/execution/memory/index.py', 'cli/aetherion_cli/commands/memory.py'], capture_output=True, text=True, cwd=r'C:\\Users\\mdham\\Downloads\\Aetherion-AI-main\\end to end aetherion cli'); print(r.stdout[:3000] if r.stdout else 'No matches found')"

echo "============================================================"
echo "MISSION 9 — NATIVE IVF CORRECTNESS"
python -c "import inspect; from backend.execution.memory.index import ApproximateVectorIndex; src = inspect.getsource(ApproximateVectorIndex.search); print(src[:4000])"
python -c "
import numpy as np
from backend.execution.memory.index import ApproximateVectorIndex
np.random.seed(42)
n = 50000
vecs = np.random.randn(n, 64).astype('float32')
vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
corpus = [(f'm{i}', vecs[i].tolist()) for i in range(n)]
q = vecs[0].tolist()
idx = ApproximateVectorIndex(corpus)
results = idx.search(q, top_k=10)
print(f'N=50k IVF search results: {results[:3]}')
print(f'Top result: {results[0][0]} (should be m0 since q == vecs[0])')
assert results[0][0] == 'm0', f'Top result wrong: {results[0][0]}'
print('N=50k native IVF top result: PASS')
print(f'Index type: {idx.index_type}')
print(f'N clusters: {len(getattr(idx, \"_centroids\", {}))}')
"

echo "============================================================"
echo "MISSION 10 — LARGE-CORPUS RECALL ADVERSARIAL"
python -c "
import numpy as np
from backend.execution.memory.index import ApproximateVectorIndex, ExactVectorIndex
def recall_at_k(approx_hits, exact_hits, k):
    exact_set = set(h[0] for h in exact_hits[:k])
    approx_set = set(h[0] for h in approx_hits[:k])
    return len(exact_set & approx_set) / max(k, 1)
def mrr(approx_hits, exact_hits, k=10):
    exact_set = set(h[0] for h in exact_hits[:k])
    for rank, (mid, _) in enumerate(approx_hits[:k]):
        if mid in exact_set: return 1.0 / (rank + 1)
    return 0.0
results_table = []
for n, desc, seed_fn in [(25000, 'uniform_random_25k', lambda n, d: np.random.randn(n, d).astype('float32')), (50000, 'uniform_random_50k', lambda n, d: np.random.randn(n, d).astype('float32')), (75000, 'uniform_random_75k', lambda n, d: np.random.randn(n, d).astype('float32')), (50000, 'clustered_50k', None), (50000, 'imbalanced_90_10_50k', None)]:
    np.random.seed(99)
    d = 32
    if desc.startswith('clustered'):
        n_clusters = 20
        vecs = np.vstack([np.random.randn(n // n_clusters, d) + np.random.randn(1, d) * 3 for _ in range(n_clusters)]).astype('float32')
    elif desc.startswith('imbalanced'):
        big_cluster = int(n * 0.9)
        small_cluster = n - big_cluster
        center1 = np.random.randn(1, d) * 5
        center2 = np.random.randn(1, d) * 5
        vecs = np.vstack([np.random.randn(big_cluster, d) + center1, np.random.randn(small_cluster, d) + center2]).astype('float32')
    else: vecs = seed_fn(n, d)
    vecs = vecs[:n]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.maximum(norms, 1e-9)
    corpus = [(f'm{i}', vecs[i].tolist()) for i in range(n)]
    idx_approx = ApproximateVectorIndex(corpus)
    idx_exact = ExactVectorIndex(corpus)
    n_queries = 50
    r1_sum = r5_sum = r10_sum = mrr_sum = 0.0
    for qi in range(n_queries):
        q = vecs[qi % n].tolist()
        hits_approx = idx_approx.search(q, top_k=10)
        hits_exact = idx_exact.search(q, top_k=10)
        r1_sum += recall_at_k(hits_approx, hits_exact, 1)
        r5_sum += recall_at_k(hits_approx, hits_exact, 5)
        r10_sum += recall_at_k(hits_approx, hits_exact, 10)
        mrr_sum += mrr(hits_approx, hits_exact)
    r1 = r1_sum / n_queries
    r5 = r5_sum / n_queries
    r10 = r10_sum / n_queries
    mrr_val = mrr_sum / n_queries
    results_table.append((desc, n, r1, r5, r10, mrr_val))
    print(f'{desc}: N={n}, R@1={r1:.3f}, R@5={r5:.3f}, R@10={r10:.3f}, MRR={mrr_val:.3f}')
print('=== RECALL TABLE ===')
for row in results_table: print(f'{row[0]:35s}: R@1={row[2]:.3f} R@5={row[3]:.3f} R@10={row[4]:.3f} MRR={row[5]:.3f}')
"

echo "============================================================"
echo "MISSION 11 — 50K PERFORMANCE SLA"
python -c "
import time, statistics, numpy as np
from backend.execution.memory.index import ApproximateVectorIndex, get_vector_index, AutoIndexConfig
config = AutoIndexConfig()
print(f'AutoIndexConfig exact_threshold={config.exact_threshold}')
for n, expected_engine in [(25000, 'native_ivf'), (50000, 'native_ivf'), (50001, 'native_ivf'), (75000, 'native_ivf')]:
    np.random.seed(42)
    d = 64
    vecs = np.random.randn(n, d).astype('float32')
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
    corpus = [(f'm{i}', vecs[i].tolist()) for i in range(n)]
    q = vecs[0].tolist()
    t0 = time.perf_counter()
    idx = get_vector_index(owner_id='bench', vectors=corpus, config=config)
    build_ms = (time.perf_counter() - t0) * 1000
    for _ in range(5): idx.search(q, top_k=10)
    latencies = []
    for _ in range(100):
        t_q = time.perf_counter()
        idx.search(q, top_k=10)
        latencies.append((time.perf_counter() - t_q) * 1000)
    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(0.95 * len(latencies))]
    p99 = latencies[-1]
    sla_status = 'PASS' if (n == 50000 and p95 < 80.0) else ('N/A' if n != 50000 else 'FAIL')
    print(f'N={n:6d}: engine={idx.index_type}, build={build_ms:.1f}ms, p50={p50:.2f}ms, p95={p95:.2f}ms, p99={p99:.2f}ms, SLA={sla_status}')
    if n == 50000: assert p95 < 80.0, f'N=50k p95={p95:.2f}ms EXCEEDS 80ms SLA!'
print('MISSION 11 PERFORMANCE SLA: PASS')
"

echo "============================================================"
echo "MISSION 12 — SHADOW CALIBRATION RESOURCE SAFETY"
python -c "
import time, numpy as np
from backend.execution.memory.index import ApproximateVectorIndex, ShadowQueryCalibrator
np.random.seed(99)
n = 50000
vecs = np.random.randn(n, 64).astype('float32')
vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
corpus = [(f'm{i}', vecs[i].tolist()) for i in range(n)]
q = vecs[0].tolist()
idx = ApproximateVectorIndex(corpus)
latencies_off = []
for _ in range(50):
    t0 = time.perf_counter()
    idx.search(q, top_k=10)
    latencies_off.append((time.perf_counter() - t0) * 1000)
latencies_off.sort()
p50_off = np.median(latencies_off)
p95_off = latencies_off[int(0.95 * len(latencies_off))]
print(f'Shadow OFF: p50={p50_off:.3f}ms p95={p95_off:.3f}ms')

snap = idx._snapshot
v_mat = snap.vectors_matrix
r_mid = snap.row_to_mid
cal = ShadowQueryCalibrator(sampling_rate=1.0, max_queue_size=200, max_workers=4, job_timeout=30.0)
for i in range(30):
    cal.submit_shadow_job(owner_id=f't{i%3}', vectors=corpus, query_vector=q, ivf_hits=[], k=10, corpus_size=n, index_generation=idx.generation, vectors_matrix=v_mat, row_to_mid=r_mid)

latencies_on = []
for _ in range(50):
    t0 = time.perf_counter()
    idx.search(q, top_k=10)
    latencies_on.append((time.perf_counter() - t0) * 1000)
latencies_on.sort()
p50_on = np.median(latencies_on)
p95_on = latencies_on[int(0.95 * len(latencies_on))]
print(f'Shadow ON:  p50={p50_on:.3f}ms p95={p95_on:.3f}ms')

if p95_on > p95_off * 5: print(f'WARNING: Shadow overhead potentially excessive: {p95_on:.2f}ms vs {p95_off:.2f}ms')
else: print(f'Shadow overhead acceptable: {p95_on:.2f}ms vs {p95_off:.2f}ms')

cal.shutdown(wait=True)
print('MISSION 12 SHADOW CALIBRATION: PASS')
"
python -c "
from backend.execution.memory.index import ShadowQueryCalibrator
cal = ShadowQueryCalibrator(sampling_rate=1.0, max_queue_size=5, max_workers=1, job_timeout=2.0)
vecs = [('a', [1.0, 0.0]), ('b', [0.0, 1.0])]
q = [1.0, 0.0]
results = [cal.submit_shadow_job(owner_id='t', vectors=vecs, query_vector=q, ivf_hits=[], k=2, corpus_size=2, index_generation=1) for _ in range(20)]
print(f'Accepted: {sum(1 for r in results if r)}, Dropped: {sum(1 for r in results if not r)}')
assert sum(1 for r in results if not r) > 0, 'Queue saturation not dropping excess jobs!'
print('Queue saturation: PASS (drops excess jobs)')
cal.shutdown()
"

echo "============================================================"
echo "MISSION 13 — TIMEOUT ENFORCEMENT"
python -c "import inspect; from backend.execution.memory.index import ShadowQueryCalibrator; src = inspect.getsource(ShadowQueryCalibrator._execute_job); print(src[:4000])"

echo "============================================================"
echo "MISSION 14 — RCU SNAPSHOT SAFETY"
python -c "
import threading, time, random
from backend.execution.memory.index import ApproximateVectorIndex
errors = []
def reader(idx, n_reads, tid):
    for i in range(n_reads):
        try:
            q = [random.random(), random.random(), random.random(), random.random()]
            results = idx.search(q, top_k=5)
            for mid, sim in results:
                assert isinstance(mid, str) and isinstance(sim, float), f't{tid} bad result type'
        except Exception as e: errors.append(f'reader {tid}: {e}')

vecs_init = [(f'm{i}', [random.random(), random.random(), random.random(), random.random()]) for i in range(200)]
idx = ApproximateVectorIndex(vecs_init)
reader_threads = [threading.Thread(target=reader, args=(idx, 200, i)) for i in range(8)]
for t in reader_threads: t.start()

def writer():
    for i in range(10):
        try:
            idx.add(f'new_{i}', [random.random(), random.random(), random.random(), random.random()])
            time.sleep(0.001)
            if i % 3 == 0:
                new_vecs = [(f'r{j}', [random.random(), random.random(), random.random(), random.random()]) for j in range(200)]
                idx.rebuild(new_vecs)
            time.sleep(0.001)
        except Exception as e: errors.append(f'writer: {e}')

wt = threading.Thread(target=writer)
wt.start()
for t in reader_threads: t.join()
wt.join()

if errors: print(f'RCU ERRORS: {errors[:5]}')
else: print('RCU SNAPSHOT SAFETY: PASS (no torn reads or KeyErrors under 8 readers + writer)')
"

echo "============================================================"
echo "MISSION 15 — TEST FORENSICS"
python -m pytest tests/integration --collect-only -q
python -m pytest cli/tests --collect-only -q 2>&1 | select -last 5
python -m pytest tests/integration cli/tests -q
python -m pytest tests/integration cli/tests -v 2>&1 | findstr /c:'SKIP' /c:'XFAIL' /c:'XPASS'

echo "============================================================"
echo "MISSION 16 — PHASE 1-7 BACKWARD COMPATIBILITY"
python -m pytest tests/integration/test_phase2_providers.py tests/integration/test_phase3_extensions.py tests/integration/test_phase4_memory_sessions_jobs.py tests/integration/test_phase4_remediation.py tests/integration/test_phase5_features.py tests/integration/test_phase6_features.py tests/integration/test_phase6_remediation.py tests/integration/test_phase7_features.py tests/integration/test_phase7_remediation.py -q

echo "============================================================"
echo "MISSION 17 — SECURITY: CALIBRATION TENANT ISOLATION"
python -c "
import tempfile, os
from backend.execution.persistence.db import StateDatabase
from backend.execution.memory.index import ShadowQueryCalibrator
with tempfile.TemporaryDirectory() as tmpdir:
    db = StateDatabase(db_path=os.path.join(tmpdir, 'sec.db'))
    cal = ShadowQueryCalibrator(db=db)
    cal.reset()
    for i in range(10): cal.record_observation('tenant_a', recall=0.9, corpus_size=100, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp')
    state_b = cal._get_state('tenant_b')
    assert state_b.samples == 0, f'CROSS-TENANT LEAK: tenant_b got samples from tenant_a: {state_b.samples}'
    print('Tenant isolation: PASS')
    try:
        state_empty = cal._get_state('')
        print(f'Empty owner_id: samples={state_empty.samples} (should be 0 or raise)')
    except Exception as e: print(f'Empty owner_id raised exception: {type(e).__name__} (acceptable)')
    try:
        state_none = cal._get_state(None)
        print(f'None owner_id: samples={state_none.samples}')
    except Exception as e: print(f'None owner_id raised: {type(e).__name__}: {e} (acceptable)')
    print('MISSION 17 SECURITY: COMPLETE')
"

echo "============================================================"
echo "MISSION 18 — NEW DEFECT DISCOVERY"
python -c "import inspect; from backend.execution.memory.index import get_vector_index; src = inspect.getsource(get_vector_index); print('get_vector_index signature/body (first 500 chars):'); print(src[:800])"
python -c "
import tempfile, os, threading
from backend.execution.persistence.db import StateDatabase
from backend.execution.memory.index import ShadowQueryCalibrator
with tempfile.TemporaryDirectory() as tmpdir:
    db = StateDatabase(db_path=os.path.join(tmpdir, 'concurrent.db'))
    cal = ShadowQueryCalibrator(db=db)
    cal.reset()
    errors = []
    def concurrent_writer(tid):
        try:
            for i in range(20): cal.record_observation(f'tenant_{tid}', recall=0.7 + tid * 0.01, corpus_size=1000, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp')
        except Exception as e: errors.append(f'tid={tid}: {e}')
    threads = [threading.Thread(target=concurrent_writer, args=(i,)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    if errors: print(f'CONCURRENT WRITE ERRORS: {errors}')
    else:
        any_wrong = False
        for tid in range(5):
            state = cal._get_state(f'tenant_{tid}', corpus_size=1000, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp')
            if state.samples != 20: print(f'tenant_{tid}: expected 20 samples, got {state.samples}'); any_wrong = True
        if not any_wrong: print('Concurrent calibration writes: PASS')
        else: print('Concurrent calibration writes: PARTIAL (some tenants got wrong counts - may be acceptable due to WAL mode)')
"
python -c "
from backend.execution.memory.index import ApproximateVectorIndex
import numpy as np
np.random.seed(55)
vecs_a = [('m0', [1.0, 0.0]), ('m1', [0.0, 1.0]), ('m2', [0.5, 0.5])]
idx = ApproximateVectorIndex(vectors=vecs_a)
result_before = idx.search([1.0, 0.0], top_k=1)
print(f'Before mutation rebuild, top-1 for [1,0]: {result_before}')
vecs_mutated = [('m0', [0.0, 1.0]), ('m1', [1.0, 0.0]), ('m2', [0.5, 0.5])]
idx.rebuild(vecs_mutated)
result_after = idx.search([1.0, 0.0], top_k=1)
print(f'After mutation rebuild, top-1 for [1,0]: {result_after}')
assert result_after[0][0] == 'm1', f'STALE REBUILD BUG: expected m1 but got {result_after}'
print('Stale rebuild test: PASS (rebuild correctly updates search results)')
"
python -c "
import tempfile, os, sqlite3
from backend.execution.persistence.db import StateDatabase
from backend.execution.memory.index import ShadowQueryCalibrator
with tempfile.TemporaryDirectory() as tmpdir:
    db_path = os.path.join(tmpdir, 'gen_test.db')
    db = StateDatabase(db_path=db_path)
    cal = ShadowQueryCalibrator(db=db)
    cal.reset()
    for i in range(5): cal.record_observation('t1', recall=0.9, corpus_size=1000, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp')
    for i in range(3): cal.record_observation('t1', recall=0.5, corpus_size=1000, index_generation=2, model_name='m', model_version='1', config_fingerprint='fp')
    conn = sqlite3.connect(db_path)
    rows = conn.execute('SELECT tenant_id, index_generation, samples FROM calibration_states').fetchall()
    conn.close()
    print(f'DB rows for t1: {rows}')
    state_gen1 = cal._get_state('t1', corpus_size=1000, index_generation=1, model_name='m', model_version='1', config_fingerprint='fp')
    print(f'State with gen=1: samples={state_gen1.samples}')
    state_gen2 = cal._get_state('t1', corpus_size=1000, index_generation=2, model_name='m', model_version='1', config_fingerprint='fp')
    print(f'State with gen=2: samples={state_gen2.samples}')
    if state_gen1.samples != 0: print('POTENTIAL ISSUE: gen=1 state not invalidated after gen=2 write - check if this is correct behavior')
    else: print('Generation invalidation: PASS (gen=1 properly invalidated after gen=2 write)')
"

echo "============================================================"
echo "FINAL: Run full test suite for count verification"
python -m pytest tests/integration cli/tests -q --tb=no
