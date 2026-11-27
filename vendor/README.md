# vendor/

## ergo_lib_python 0.29.0 (Python 3.10, Linux x86_64)

`ergo_lib_python-0.29.0-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`

The transaction library the apps use, built from source because the version on PyPI (0.28.0)
cannot read or evaluate contracts that use Ergo 6.0 operations, which Lithos's contracts do.
With 0.29 the helper can check LithosDex orders and cancellations against the real contracts
before a wallet signs them. Everything else behaves the same: the swap, order, liquidity, mint
and Lithos builders produced identical transactions under both versions when this was added.

- Source: https://github.com/ergoplatform/sigma-rust, branch `develop`, commit `e868934`
  (16 Nov 2026), `bindings/ergo-lib-python`. Licence CC0-1.0. Unreleased at the time.
- With one change, `sigma-rust-lazy-context.patch` in this folder (seven lines in
  `ergotree-ir/src/mir/expr.rs`): a script that runs an expression supplied by the spender
  (`executeFromVar`) no longer fails just because that expression is absent on a path that never
  reads it. The Ergo network's own interpreter behaves this way; without the change the SigmaFi
  repay, cancel and unguarded-fill transactions cannot be prepared at all. Nothing else differs.
- SHA-256: `704712a164a07c35268586aace9023ac03bd1eca04311e7beabd2a71eecd80c0`
  (the earlier build without the patch was `3b6e5df6...`; reinstall with `--force-reinstall`,
  since the version number is the same)
- Needs CPython 3.10 and glibc 2.17 or newer (any current Linux).

Install into the app's virtualenv, then check:

```bash
cd /usr/lib/cgi-bin/sigmaswap
sha256sum vendor/ergo_lib_python-0.29.0-cp310-*.whl          # must match the line above
sudo venv/bin/python -m pip install --force-reinstall --no-deps vendor/ergo_lib_python-0.29.0-cp310-*.whl
sudo ./redeploy.sh
venv/bin/python -I -c 'import ergo_lib_python as e; print(e.__version__)'     # 0.29.0
```

To go back: `sudo venv/bin/python -m pip install ergo-lib-python==0.28.0 && sudo ./redeploy.sh`.

Build it yourself instead of trusting this file (needs Rust and Python 3.10):

```bash
git clone https://github.com/ergoplatform/sigma-rust && cd sigma-rust && git checkout e868934
git apply /usr/lib/cgi-bin/sigmaswap/vendor/sigma-rust-lazy-context.patch
cd bindings/ergo-lib-python
pip install "maturin>=1.7,<2.0" ziglang
maturin build --release --zig --compatibility manylinux2014 -i python3.10
```

Once 0.29 is published on PyPI this file can go and `requirements.txt` can name it.
