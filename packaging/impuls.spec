%global buildsubdir impuls-%{version}

Name:           impuls
Version:        1.0.1
Release:        1%{?dist}
Summary:        Independent retro-styled Yandex Music player
License:        GPL-3.0-only AND LGPL-3.0-only
URL:            https://github.com/AnoY606-git/Impuls
Source0:        %{name}-%{version}.tar.gz
Source1:        yandex_music-3.2.2-py3-none-any.whl
BuildArch:      noarch
BuildRequires:  python3
BuildRequires:  python3-gobject
BuildRequires:  python3-cairo
BuildRequires:  python3-requests
BuildRequires:  python3-pysocks
BuildRequires:  python3-typing-extensions
BuildRequires:  gtk4
BuildRequires:  gobject-introspection
BuildRequires:  gstreamer1
BuildRequires:  gstreamer1-plugins-good
BuildRequires:  libsecret
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       python3-requests
Requires:       python3-pysocks
Requires:       python3-typing-extensions
Requires:       gtk4
Requires:       gobject-introspection
Requires:       adwaita-icon-theme
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       gstreamer1-plugins-bad-free
Requires:       glib-networking
Requires:       libsecret
Provides:       bundled(python3dist(yandex-music)) = 3.2.2

%description
Impuls is an independent GTK music player for Fedora. It connects to Yandex
Music using the unofficial yandex-music library.

%prep
tar -xzf %{SOURCE0}

%build
# No compilation is required.

%install
install -d %{buildroot}%{_datadir}/impuls/impuls_player
cp -a impuls_player/*.py impuls_player/style.css %{buildroot}%{_datadir}/impuls/impuls_player/
install -d %{buildroot}%{_datadir}/impuls/vendor
%{__python3} -m zipfile -e %{SOURCE1} %{buildroot}%{_datadir}/impuls/vendor
install -Dpm 0755 data/impuls %{buildroot}%{_bindir}/impuls
install -Dpm 0644 data/io.github.AnoY606_git.Impuls.desktop %{buildroot}%{_datadir}/applications/io.github.AnoY606_git.Impuls.desktop
install -Dpm 0644 data/io.github.AnoY606_git.Impuls.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.AnoY606_git.Impuls.svg
install -Dpm 0644 data/io.github.AnoY606_git.Impuls.metainfo.xml %{buildroot}%{_datadir}/metainfo/io.github.AnoY606_git.Impuls.metainfo.xml
install -Dpm 0644 data/io.github.AnoY606_git.Impuls.svg %{buildroot}%{_docdir}/impuls/data/io.github.AnoY606_git.Impuls.svg
install -Dpm 0644 LICENSE %{buildroot}%{_docdir}/impuls/LICENSE

%check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:%{buildroot}%{_datadir}/impuls/vendor %{__python3} -m unittest discover -s tests -q

%files
%license LICENSE
%doc README.md docs
%doc %{_docdir}/impuls/data/io.github.AnoY606_git.Impuls.svg
%doc %{_docdir}/impuls/LICENSE
%{_bindir}/impuls
%{_datadir}/impuls/
%{_datadir}/applications/io.github.AnoY606_git.Impuls.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.AnoY606_git.Impuls.svg
%{_datadir}/metainfo/io.github.AnoY606_git.Impuls.metainfo.xml

%changelog
* Tue Nov 03 2026 AnoY606-git and contributors - 1.0.1-1
- Load collections incrementally, cache metadata, improve responsive UI
- Prefetch upcoming audio in memory, select quality and recover stalled streams

* Tue Nov 03 2026 AnoY606-git and contributors - 1.0-1
- Initial release
