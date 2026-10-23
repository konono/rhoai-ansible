# Release と self update

`aw` と同じく、GitHub Release の配布物を更新元にする。ここでは実行ファイルではなく `rhoai-3.5-ansible/` のソースを配布する。

## Release を作る

`main` に変更を取り込んだ後、`rhoai-3.5-ansible/VERSION` と `pyproject.toml` のバージョンを同じ値にする。`vX.Y.Z` タグをその commit に付けて push すると `.github/workflows/release.yml` が動く。

```bash
git checkout main
git pull --ff-only
# VERSION と pyproject.toml のバージョンを更新して commit
git tag vX.Y.Z
git push origin vX.Y.Z
```

Workflow はタグと `VERSION` の一致、および updater のテストを確認する。タグの Git tree からソース tarball と `checksums.txt` を作り、検証後に GitHub Release へ添付する。作業ツリーの未追跡ファイルや環境固有 inventory は配布物に入らない。Release の公開はタグ push によって行われるため、タグを push する前に内容を確認すること。

## 更新する

インストール先の `rhoai-3.5-ansible/` で実行する。

```bash
python3 scripts/self-update.py          # 最新 Release と変更一覧を確認
python3 scripts/self-update.py --apply  # 更新を適用
```

オフライン環境では Release ページから `rhoai-3.5-ansible.tar.gz` と `checksums.txt` を別の端末で取得して持ち込む。従来の `pack-playbook.sh` の tarball には Release 用 manifest がないので、このコマンドには使わない。

```bash
python3 scripts/self-update.py --archive /media/rhoai-3.5-ansible.tar.gz --checksums /media/checksums.txt
python3 scripts/self-update.py --archive /media/rhoai-3.5-ansible.tar.gz --checksums /media/checksums.txt --apply
```

updater は tarball の SHA-256 とファイル一覧を検証する。Release に含まれていた旧ソースで、新版から消えたものだけを削除する。環境固有の inventory、資格情報、仮想環境、vendor は更新対象外。ソース更新だけを行い、クラスタ上のリソースは変更しない。

Git checkout 内では `--apply` を拒否する。開発用 checkout は `git pull` で更新する。
