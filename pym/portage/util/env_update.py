# Copyright 2010-2011 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

__all__ = ['env_update']

import errno
import io
import stat
import sys
import time

import portage
from portage import os, _encodings, _unicode_encode
from portage.checksum import prelink_capable
from portage.data import ostype
from portage.exception import ParseError
from portage.localization import _
from portage.process import find_binary
from portage.util import atomic_ofstream, ensure_dirs, getconfig, \
	normalize_path, writemsg
from portage.util.listdir import listdir
from portage.dbapi.vartree import vartree
from portage.package.ebuild.config import config

if sys.hexversion >= 0x3000000:
	long = int

def env_update(makelinks=1, target_root=None, prev_mtimes=None, contents=None,
	env=None, writemsg_level=None, vardbapi=None):
	"""
	Parse /etc/env.d and use it to generate /etc/profile.env, csh.env,
	ld.so.conf, and prelink.conf. Finally, run ldconfig. When ldconfig is
	called, its -X option will be used in order to avoid potential
	interference with installed soname symlinks that are required for
	correct operation of FEATURES=preserve-libs for downgrade operations.
	It's not necessary for ldconfig to create soname symlinks, since
	portage will use NEEDED.ELF.2 data to automatically create them
	after src_install if they happen to be missing.
	@param makelinks: True if ldconfig should be called, False otherwise
	@param target_root: root that is passed to the ldconfig -r option,
		defaults to portage.settings["ROOT"].
	@type target_root: String (Path)
	"""
	if vardbapi is None:
		if isinstance(env, config):
			vardbapi = vartree(settings=env).dbapi
		else:
			if target_root is None:
				eprefix = portage.settings["EPREFIX"]
				target_root = portage.settings["ROOT"]
				target_eroot = portage.settings['EROOT']
			else:
				eprefix = portage.const.EPREFIX
				target_eroot = os.path.join(target_root,
					eprefix.lstrip(os.sep))
				target_eroot = target_eroot.rstrip(os.sep) + os.sep
			if hasattr(portage, "db") and target_eroot in portage.db:
				vardbapi = portage.db[target_eroot]["vartree"].dbapi
			else:
				settings = config(config_root=target_root,
					target_root=target_root, eprefix=eprefix)
				target_root = settings["ROOT"]
				if env is None:
					env = settings
				vardbapi = vartree(settings=settings).dbapi

	# Lock the config memory file to prevent symlink creation
	# in merge_contents from overlapping with env-update.
	vardbapi._fs_lock()
	try:
		return _env_update(makelinks, target_root, prev_mtimes, contents,
			env, writemsg_level)
	finally:
		vardbapi._fs_unlock()

def _env_update(makelinks, target_root, prev_mtimes, contents, env,
	writemsg_level):
	if writemsg_level is None:
		writemsg_level = portage.util.writemsg_level
	if target_root is None:
		target_root = portage.settings["ROOT"]
	if prev_mtimes is None:
		prev_mtimes = portage.mtimedb["ldpath"]
	if env is None:
		settings = portage.settings
	else:
		settings = env

	eprefix = settings.get("EPREFIX", "")
	eprefix_lstrip = eprefix.lstrip(os.sep)
	envd_dir = os.path.join(target_root, eprefix_lstrip, "etc", "env.d")
	ensure_dirs(envd_dir, mode=0o755)
	fns = listdir(envd_dir, EmptyOnError=1)
	fns.sort()
	templist = []
	for x in fns:
		if len(x) < 3:
			continue
		if not x[0].isdigit() or not x[1].isdigit():
			continue
		if x.startswith(".") or x.endswith("~") or x.endswith(".bak"):
			continue
		templist.append(x)
	fns = templist
	del templist

	space_separated = set(["CONFIG_PROTECT", "CONFIG_PROTECT_MASK"])
	colon_separated = set(["ADA_INCLUDE_PATH", "ADA_OBJECTS_PATH",
		"CLASSPATH", "INFODIR", "INFOPATH", "KDEDIRS", "LDPATH", "MANPATH",
		  "PATH", "PKG_CONFIG_PATH", "PRELINK_PATH", "PRELINK_PATH_MASK",
		  "PYTHONPATH", "ROOTPATH"])

	config_list = []

	for x in fns:
		file_path = os.path.join(envd_dir, x)
		try:
			myconfig = getconfig(file_path, expand=False)
		except ParseError as e:
			writemsg("!!! '%s'\n" % str(e), noiselevel=-1)
			del e
			continue
		if myconfig is None:
			# broken symlink or file removed by a concurrent process
			writemsg("!!! File Not Found: '%s'\n" % file_path, noiselevel=-1)
			continue

		config_list.append(myconfig)
		if "SPACE_SEPARATED" in myconfig:
			space_separated.update(myconfig["SPACE_SEPARATED"].split())
			del myconfig["SPACE_SEPARATED"]
		if "COLON_SEPARATED" in myconfig:
			colon_separated.update(myconfig["COLON_SEPARATED"].split())
			del myconfig["COLON_SEPARATED"]

	env = {}
	specials = {}
	for var in space_separated:
		mylist = []
		for myconfig in config_list:
			if var in myconfig:
				for item in myconfig[var].split():
					if item and not item in mylist:
						mylist.append(item)
				del myconfig[var] # prepare for env.update(myconfig)
		if mylist:
			env[var] = " ".join(mylist)
		specials[var] = mylist

	for var in colon_separated:
		mylist = []
		for myconfig in config_list:
			if var in myconfig:
				for item in myconfig[var].split(":"):
					if item and not item in mylist:
						mylist.append(item)
				del myconfig[var] # prepare for env.update(myconfig)
		if mylist:
			env[var] = ":".join(mylist)
		specials[var] = mylist

	for myconfig in config_list:
		"""Cumulative variables have already been deleted from myconfig so that
		they won't be overwritten by this dict.update call."""
		env.update(myconfig)

	ldsoconf_path = os.path.join(
		target_root, eprefix_lstrip, "etc", "ld.so.conf")
	try:
		myld = io.open(_unicode_encode(ldsoconf_path,
			encoding=_encodings['fs'], errors='strict'),
			mode='r', encoding=_encodings['content'], errors='replace')
		myldlines=myld.readlines()
		myld.close()
		oldld=[]
		for x in myldlines:
			#each line has at least one char (a newline)
			if x[:1] == "#":
				continue
			oldld.append(x[:-1])
	except (IOError, OSError) as e:
		if e.errno != errno.ENOENT:
			raise
		oldld = None

	newld = specials["LDPATH"]
	if (oldld != newld):
		#ld.so.conf needs updating and ldconfig needs to be run
		myfd = atomic_ofstream(ldsoconf_path)
		myfd.write("# ld.so.conf autogenerated by env-update; make all changes to\n")
		myfd.write("# contents of /etc/env.d directory\n")
		for x in specials["LDPATH"]:
			myfd.write(x + "\n")
		myfd.close()

	# Update prelink.conf if we are prelink-enabled
	if prelink_capable:
		newprelink = atomic_ofstream(os.path.join(
			target_root, eprefix_lstrip, "etc", "prelink.conf"))
		newprelink.write("# prelink.conf autogenerated by env-update; make all changes to\n")
		newprelink.write("# contents of /etc/env.d directory\n")

		for x in ["/bin","/sbin","/usr/bin","/usr/sbin","/lib","/usr/lib"]:
			newprelink.write("-l %s\n" % (x,));
		prelink_paths = []
		prelink_paths += specials.get("LDPATH", [])
		prelink_paths += specials.get("PATH", [])
		prelink_paths += specials.get("PRELINK_PATH", [])
		prelink_path_mask = specials.get("PRELINK_PATH_MASK", [])
		for x in prelink_paths:
			if not x:
				continue
			if x[-1:] != '/':
				x += "/"
			plmasked = 0
			for y in prelink_path_mask:
				if not y:
					continue
				if y[-1] != '/':
					y += "/"
				if y == x[0:len(y)]:
					plmasked = 1
					break
			if not plmasked:
				newprelink.write("-h %s\n" % (x,))
		for x in prelink_path_mask:
			newprelink.write("-b %s\n" % (x,))
		newprelink.close()

	current_time = long(time.time())
	mtime_changed = False
	lib_dirs = set()
	for lib_dir in set(specials["LDPATH"] + \
		['usr/lib','usr/lib64','usr/lib32','lib','lib64','lib32']):
		x = os.path.join(target_root, eprefix_lstrip, lib_dir.lstrip(os.sep))
		try:
			newldpathtime = os.stat(x)[stat.ST_MTIME]
			lib_dirs.add(normalize_path(x))
		except OSError as oe:
			if oe.errno == errno.ENOENT:
				try:
					del prev_mtimes[x]
				except KeyError:
					pass
				# ignore this path because it doesn't exist
				continue
			raise
		if newldpathtime == current_time:
			# Reset mtime to avoid the potential ambiguity of times that
			# differ by less than 1 second.
			newldpathtime -= 1
			os.utime(x, (newldpathtime, newldpathtime))
			prev_mtimes[x] = newldpathtime
			mtime_changed = True
		elif x in prev_mtimes:
			if prev_mtimes[x] == newldpathtime:
				pass
			else:
				prev_mtimes[x] = newldpathtime
				mtime_changed = True
		else:
			prev_mtimes[x] = newldpathtime
			mtime_changed = True

	if makelinks and \
		not mtime_changed and \
		contents is not None:
		libdir_contents_changed = False
		for mypath, mydata in contents.items():
			if mydata[0] not in ("obj", "sym"):
				continue
			head, tail = os.path.split(mypath)
			if head in lib_dirs:
				libdir_contents_changed = True
				break
		if not libdir_contents_changed:
			makelinks = False

	ldconfig = "/sbin/ldconfig"
	if "CHOST" in settings and "CBUILD" in settings and \
		settings["CHOST"] != settings["CBUILD"]:
		ldconfig = find_binary("%s-ldconfig" % settings["CHOST"])

	# Only run ldconfig as needed
	if makelinks and ldconfig and not eprefix:
		# ldconfig has very different behaviour between FreeBSD and Linux
		if ostype == "Linux" or ostype.lower().endswith("gnu"):
			# We can't update links if we haven't cleaned other versions first, as
			# an older package installed ON TOP of a newer version will cause ldconfig
			# to overwrite the symlinks we just made. -X means no links. After 'clean'
			# we can safely create links.
			writemsg_level(_(">>> Regenerating %setc/ld.so.cache...\n") % \
				(target_root,))
			os.system("cd / ; %s -X -r '%s'" % (ldconfig, target_root))
		elif ostype in ("FreeBSD","DragonFly"):
			writemsg_level(_(">>> Regenerating %svar/run/ld-elf.so.hints...\n") % \
				target_root)
			os.system(("cd / ; %s -elf -i " + \
				"-f '%svar/run/ld-elf.so.hints' '%setc/ld.so.conf'") % \
				(ldconfig, target_root, target_root))

	del specials["LDPATH"]

	penvnotice  = "# THIS FILE IS AUTOMATICALLY GENERATED BY env-update.\n"
	penvnotice += "# DO NOT EDIT THIS FILE. CHANGES TO STARTUP PROFILES\n"
	cenvnotice  = penvnotice[:]
	penvnotice += "# GO INTO /etc/profile NOT /etc/profile.env\n\n"
	cenvnotice += "# GO INTO /etc/csh.cshrc NOT /etc/csh.env\n\n"

	#create /etc/profile.env for bash support
	outfile = atomic_ofstream(os.path.join(
		target_root, eprefix_lstrip, "etc", "profile.env"))
	outfile.write(penvnotice)

	env_keys = [ x for x in env if x != "LDPATH" ]
	env_keys.sort()
	for k in env_keys:
		v = env[k]
		if v.startswith('$') and not v.startswith('${'):
			outfile.write("export %s=$'%s'\n" % (k, v[1:]))
		else:
			outfile.write("export %s='%s'\n" % (k, v))
	outfile.close()

	#create /etc/csh.env for (t)csh support
	outfile = atomic_ofstream(os.path.join(
		target_root, eprefix_lstrip, "etc", "csh.env"))
	outfile.write(cenvnotice)
	for x in env_keys:
		outfile.write("setenv %s '%s'\n" % (x, env[x]))
	outfile.close()