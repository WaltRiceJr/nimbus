# SPDX-License-Identifier: GPL-3.0-or-later

Name:           nimbus-weather
Version:        1.0.0
Release:        1%{?dist}
Summary:        Weather for the United States, from the National Weather Service

License:        GPL-3.0-or-later
URL:            https://github.com/WaltRiceJr/nimbus
Source0:        nimbus-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils

Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       libadwaita

%description
A weather application for GNOME, backed by the United States National
Weather Service. Every weather symbol and sky scene is drawn as vector art
at runtime: the sky gradient follows the sun's real altitude, the moon is
shown at its true phase, and rain, snow, fog and lightning animate over
drifting cloud layers. Includes a 48-hour forecast strip, a 7-day outlook,
animated radar with satellite cloud cover, and NWS watches and warnings.

%prep
%autosetup -n nimbus-%{version}

# -R: runtime requirements stay out of the build requirements. Building the
# wheel never imports GTK, and the binary package's Requires are declared
# explicitly above against the distro's own package names.
%generate_buildrequires
%pyproject_buildrequires -R

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files nimbus

install -Dm0644 data/org.nimbus.Weather.metainfo.xml \
    %{buildroot}%{_metainfodir}/org.nimbus.Weather.metainfo.xml

sed 's|@EXEC@|nimbus-weather|' data/org.nimbus.Weather.desktop.in \
    > org.nimbus.Weather.desktop
desktop-file-install --dir=%{buildroot}%{_datadir}/applications \
    org.nimbus.Weather.desktop

for size in 16 24 32 48 64 128 256 512; do
    install -Dm0644 data/icons/nimbus-${size}.png \
        %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/org.nimbus.Weather.png
done

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/nimbus-weather
%{_datadir}/applications/org.nimbus.Weather.desktop
%{_metainfodir}/org.nimbus.Weather.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/org.nimbus.Weather.png

%changelog
* Fri Jul 31 2026 Walter Rice - 1.0.0-1
- Initial package
