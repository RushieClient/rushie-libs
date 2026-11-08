#!/usr/bin/env python3
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen
import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile

REPOSITORY = "https://api.github.com/repos/livekit/client-sdk-cpp"
PLATFORMS = {
	"ubuntu-22.04-x64": "linux/lib64",
	"ubuntu-22.04-arm64": "linux/libarm64",
	"macos-x64": "mac/lib64",
	"macos-arm64": "mac/libarm64",
	"windows-x64": "windows/lib64",
}


def open_url(url):
	for attempt in range(3):
		try:
			return urlopen(Request(url, headers={"User-Agent": "RushieClient-LiveKit-Updater"}), timeout=120)
		except (OSError, HTTPException):
			if attempt == 2:
				raise
			time.sleep(attempt + 1)


def download_archive(asset, archive):
	for attempt in range(3):
		try:
			digest = hashlib.sha256()
			with open_url(asset["browser_download_url"]) as response, archive.open("wb") as output:
				while chunk := response.read(1024 * 1024):
					digest.update(chunk)
					output.write(chunk)
		except (OSError, HTTPException):
			if attempt == 2:
				raise
			print(f"Download interrupted; retrying {archive.name}...", flush=True)
			continue
		if digest.hexdigest() != asset["digest"].removeprefix("sha256:"):
			raise RuntimeError(f"SHA256 mismatch: {archive.name}")
		return


def unpack_archive(archive, destination):
	def write_file(name, source):
		path = PurePosixPath(name)
		if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
			raise RuntimeError(f"Unsafe archive path: {name}")
		target = destination.joinpath(*path.parts)
		target.parent.mkdir(parents=True, exist_ok=True)
		with source, target.open("wb") as output:
			shutil.copyfileobj(source, output)

	if archive.suffix == ".zip":
		with zipfile.ZipFile(archive) as package:
			for entry in package.infolist():
				if not entry.is_dir():
					write_file(entry.filename, package.open(entry))
	else:
		with tarfile.open(archive) as package:
			for entry in package:
				if entry.isfile():
					write_file(entry.name, package.extractfile(entry))
				elif not entry.isdir():
					raise RuntimeError(f"Unsupported archive entry: {entry.name}")


def main():
	parser = argparse.ArgumentParser(description="Update all bundled LiveKit SDK platforms in rushie-libs.")
	parser.add_argument("version", help="SDK version, for example 1.13.0")
	parser.add_argument("--check", action="store_true", help="Download and validate without replacing installed files")
	args = parser.parse_args()
	version = args.version.removeprefix("v")
	if not re.fullmatch(r"\d+\.\d+\.\d+", version):
		parser.error("version must have the form 1.13.0")
	destination = Path(__file__).resolve().parent / "livekit"
	if destination.is_symlink() or destination.resolve() != destination:
		raise RuntimeError(f"Refusing to delete a redirected SDK directory: {destination}")
	if not args.check and destination.exists():
		print(f"Removing {destination}...", flush=True)
		shutil.rmtree(destination)
	with open_url(f"{REPOSITORY}/releases/tags/v{version}") as response:
		release = json.load(response)
	assets = {asset["name"]: asset for asset in release["assets"]}
	selected = []
	for platform, relative in PLATFORMS.items():
		extension = "zip" if platform == "windows-x64" else "tar.gz"
		name = f"livekit-sdk-{platform}-{version}.{extension}"
		asset = assets.get(name)
		if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
			raise RuntimeError(f"Release has no archive with a SHA256 digest: {name}")
		selected.append((platform, relative, asset))

	with tempfile.TemporaryDirectory(prefix=".livekit-update-", dir=destination.parent) as temporary:
		work = Path(temporary)
		staged = work / "staged"
		for platform, relative, asset in selected:
			print(f"Downloading {asset['name']}...", flush=True)
			archive = work / asset["name"]
			download_archive(asset, archive)
			unpacked = work / platform
			unpack_archive(archive, unpacked)
			headers = list(unpacked.glob("*/include/livekit/livekit.h"))
			if len(headers) != 1:
				raise RuntimeError(f"Unexpected SDK layout: {archive.name}")
			sdk = headers[0].parents[2]
			build = (sdk / "include/livekit/build.h").read_text(encoding="utf-8")
			if f'#define LIVEKIT_BUILD_VERSION "{version}"' not in build:
				raise RuntimeError(f"Header version mismatch: {archive.name}")
			if platform == "ubuntu-22.04-x64":
				shutil.copytree(sdk / "include", staged / "include")
			if platform == "windows-x64":
				files = ("livekit.lib", "livekit.dll", "livekit_ffi.dll")
			elif platform.startswith("macos-"):
				files = ("liblivekit.dylib", "liblivekit_ffi.dylib")
			else:
				files = ("liblivekit.so", "liblivekit_ffi.so")
			library_dir = staged / relative
			library_dir.mkdir(parents=True)
			for name in files:
				matches = [path for path in sdk.rglob(name) if path.is_file()]
				if len(matches) != 1:
					raise RuntimeError(f"Expected exactly one {name} in {archive.name}")
				shutil.copy2(matches[0], library_dir / name)
			if platform == "windows-x64":
				for path in sdk.rglob("livekit_ffi.dll.lib"):
					shutil.copy2(path, library_dir / path.name)
			print(f"Verified SHA256, headers and libraries: {platform}", flush=True)
		if args.check:
			print(f"LiveKit {version}: all five SDK archives validated; installed files unchanged.")
		else:
			staged.rename(destination)
			print(f"Updated LiveKit {version}: {destination}")
			print("Review and commit rushie-libs, then update its submodule reference in the client repository.")


if __name__ == "__main__":
	try:
		main()
	except (OSError, HTTPException, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
		print(f"ERROR: {error}", file=sys.stderr)
		sys.exit(1)
