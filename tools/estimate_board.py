#!/usr/bin/env python3
"""Estimate the board score of a commit of this repo before submitting it.

usage (from the RWX kit folder, the one with score.py and sample/):
    python3 /path/to/repo/tools/estimate_board.py <commit> [<commit> ...]

For each commit it builds the image natively for this machine, then:
  * runs it 3 times on the kit's sample commits under the scorer's limits, measuring
    the selector's CPU (user + system) and keeping the middle run, as the board does;
  * scores the output with the kit's own score.py (simulated seconds, misses);
  * runs it once on tools/bench40 (40 generated commits in the kit's mix of edit kinds,
    with their real failing tests) and scores that too.

Board estimates (fitted to our real board results; re-fit as more arrive):
  board CPU  ~= CPU_A + CPU_B * local CPU      (v3, v7, v11 measured on the board)
  board sim  ~= SIM_A + SIM_B * kit sim         (v3, v7, v11 measured on the board)
The kit model only sees changes that show on the kit's 30 commits; the benchmark line
shows changes it cannot see. Both are estimates, not the board's number.
"""
import json, os, re, statistics, subprocess, sys, tempfile

REPO_URL = "https://github.com/koillea/rwx-blast-radius-v1"
HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.getcwd()
# fitted on: v3 (Mac CPU 0.220 -> board 0.57), v7 (0.581 -> 2.17); predicts v11 0.62 (board 0.61)
CPU_A, CPU_B = -0.40, 4.43
# fitted on: v3 (kit 185.452 -> board 256.422), v7 (126.699 -> 206.153), v11 (119.652 -> 200.14)
SIM_A, SIM_B = 97.9, 0.855
FLAGS = ["--rm", "--network", "none", "--cpus", "2", "--memory", "4096m", "--read-only",
         "--tmpfs", "/tmp:rw,size=64m", "--user", "65534:65534", "--env", "HOME=/tmp"]
WRAP = ("import resource,subprocess,sys; subprocess.run(['python3','-S','/app/selector.py']); "
        "r=resource.getrusage(resource.RUSAGE_CHILDREN); sys.stderr.write('CPU %.3f\\n' % (r.ru_utime+r.ru_stime))")


def run(image, data, out_path):
    with open(out_path, "w") as out:
        p = subprocess.run(["docker", "run", *FLAGS, "--volume", f"{data}/commits:/data/commits:ro",
                            "--volume", f"{data}/tests.json:/data/tests.json:ro", "--entrypoint", "python3",
                            image, "-c", WRAP], stdout=out, stderr=subprocess.PIPE, text=True)
    m = re.search(r"CPU ([0-9.]+)", p.stderr)
    if not m:
        sys.exit("run failed:\n" + p.stderr[-2000:])
    return float(m.group(1))


def score(data, out_path):
    r = subprocess.run([sys.executable, os.path.join(KIT, "score.py"), data, f"{data}/answers.json", out_path],
                       capture_output=True, text=True).stdout
    sim = float(re.search(r"Simulated:\s+([0-9.]+)", r).group(1))
    miss = int(re.search(r"Misses:\s+(\d+)", r).group(1))
    return sim, miss


if not os.path.exists(os.path.join(KIT, "score.py")):
    sys.exit("run this from the RWX kit folder (the one with score.py and sample/)")
for commit in sys.argv[1:]:
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-q", REPO_URL, tmp], check=True)
    subprocess.run(["git", "-C", tmp, "checkout", "-q", commit], check=True)
    image = f"rwx-est-{commit[:7]}"
    subprocess.run(["docker", "build", "-q", "-t", image, tmp], check=True, stdout=subprocess.DEVNULL)
    out = os.path.join(tmp, "out.jsonl")
    cpus = [run(image, os.path.join(KIT, "sample"), out) for _ in range(3)]
    cpu = statistics.median(cpus)
    kit_sim, kit_miss = score(os.path.join(KIT, "sample"), out)
    bench = os.path.join(HERE, "bench40")
    run(image, bench, out)
    b_sim, b_miss = score(bench, out)
    est_cpu = max(0.0, CPU_A + CPU_B * cpu)
    est_sim = SIM_A + SIM_B * kit_sim + 300 * kit_miss
    print(f"\n== {commit[:7]}")
    print(f"  kit sample:      {kit_sim:.3f} s simulated, {kit_miss} misses, local CPU {cpu:.3f} s (runs {', '.join('%.3f' % c for c in cpus)})")
    print(f"  benchmark (40):  {b_sim:.3f} s simulated, {b_miss} misses   (v11 = 261.1, v12 = 203.2)")
    print(f"  board estimate:  {est_sim:.1f} s simulated + {est_cpu:.2f} s CPU = {est_sim + est_cpu:.1f} s")
    print("  (estimate; the kit model cannot see gains that only show on the benchmark)")
