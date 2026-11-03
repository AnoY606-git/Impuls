"""Release sources must never include untracked local files."""
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


class SourceArchiveTests(unittest.TestCase):
    def test_only_explicit_project_files_enter_source_archive(self):
        project = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix='impuls-source-test-') as temp:
            root = Path(temp) / 'project'
            root.mkdir()
            for folder in ('impuls_player', 'tests', 'data', 'packaging', 'scripts', 'docs'):
                shutil.copytree(project / folder, root / folder,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for filename in ('README.md', 'LICENSE'):
                shutil.copy2(project / filename, root / filename)
            # A clean checkout has no ignored review/agent working documents.
            for folder in ('reviews', 'superpowers'):
                shutil.rmtree(root / 'docs' / folder, ignore_errors=True)
            (root / 'impuls_player' / '.env').write_text('do-not-publish')
            (root / 'impuls_player' / 'experimental.py').write_text('do-not-publish')
            (root / 'tests' / 'test_untracked.py').write_text('do-not-publish')
            archive = Path(temp) / 'source.tar.gz'
            environment = dict(os.environ, IMPULS_WHEEL=str(Path(temp) / 'missing.whl'))
            result = subprocess.run(['bash', str(root / 'scripts' / 'build-rpm.sh'),
                                     '--source-only', str(archive)], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(archive, 'r:gz') as source:
                names = set(source.getnames())
            self.assertIn('impuls-1.3.0/impuls_player/app.py', names)
            self.assertIn('impuls-1.3.0/packaging/impuls.spec', names)
            self.assertFalse(any(name.endswith(('/.env', '/experimental.py', '/test_untracked.py'))
                                 for name in names))
            self.assertFalse(any('/docs/reviews/' in name or '/docs/superpowers/' in name for name in names))


if __name__ == '__main__':
    unittest.main()
