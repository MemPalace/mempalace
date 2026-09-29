from mempalace.miner import READABLE_EXTENSIONS, scan_project


CPP_EXTENSIONS = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".inl"}


def test_cpp_extensions_are_readable():
    assert CPP_EXTENSIONS <= READABLE_EXTENSIONS


def test_scan_project_includes_cpp_files_case_insensitively(tmp_path):
    expected = []
    for index, extension in enumerate(sorted(CPP_EXTENSIONS)):
        filename = f"example_{index}{extension}"
        (tmp_path / filename).write_text("int main() { return 0; }\n", encoding="utf-8")
        expected.append(filename)

    (tmp_path / "uppercase.CPP").write_text("int main() { return 0; }\n", encoding="utf-8")
    expected.append("uppercase.CPP")

    files = scan_project(str(tmp_path))
    scanned = sorted(path.relative_to(tmp_path).as_posix() for path in files)
    assert scanned == sorted(expected)
