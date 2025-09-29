from argparse import ArgumentParser
from pathlib import PosixPath
from csv import DictReader
from datetime import datetime, UTC
from html import unescape
from collections import namedtuple
from pony import orm
from tqdm import tqdm
from pickle import loads, dumps, HIGHEST_PROTOCOL
from sqlite3 import connect

db = orm.Database()
GroupInfo = namedtuple("GroupInfo", ["story_id", "group_names", "group_ids", "folder_ids", "paths"])


class KV(db.Entity):
	key = orm.PrimaryKey(str)
	value = orm.Required(bytes)

	@staticmethod
	def get(item: str):
		return loads(KV[item].value)

	@staticmethod
	def set(key: str, value):
		pickle = dumps(value, HIGHEST_PROTOCOL)
		if KV.contains(key):
			KV[key].value = pickle
		else:
			KV(key=key, value=pickle)

	@staticmethod
	def contains(item) -> bool:
		found = KV.select(key=item).count()
		return bool(found)


class Group(db.Entity):
	id = orm.PrimaryKey(int, auto=False)
	name = orm.Optional(str)
	last_checked = orm.Optional(datetime)
	stories = orm.Optional(orm.IntArray, nullable=True)
	folders = orm.Set("Folder")
	exists = orm.Required(bool, default=True)


class Folder(db.Entity):
	id = orm.PrimaryKey(int, auto=False)
	name = orm.Optional(str)
	group = orm.Optional(Group)
	parent = orm.Optional(int, index=True)
	last_checked = orm.Optional(datetime)
	exists = orm.Required(bool, default=True)
	stories = orm.Set("Placement", reverse="folder")


# this indirection is annoying, but it results in a 500X performance boost over PonyORM.IntArray, so it can't be helped
class Placement(db.Entity):
	story = orm.Required(int, index=True)
	folder = orm.Required(Folder, index=True)


class GroupMeta:
	def __init__(self, filename: PosixPath = PosixPath("folders.sqlite")):
		file_path = str(filename)
		db.bind(provider="sqlite", filename=file_path, create_db=True)
		db.generate_mapping(create_tables=True)
		with orm.db_session:
			if KV.contains("ready"):
				self.ready = KV.get("ready")
				return
			try:
				stub_group = Group[0]
			except orm.ObjectNotFound:
				stub_group = Group(id=0, name="stub")
			try:
				Folder[0]
			except orm.ObjectNotFound:
				Folder(id=0, name="stub", parent=0, stories=[], group=stub_group)
			KV.set("ready", False)
			self.ready = False

	@classmethod
	@orm.db_session
	def scan_directory(cls, groups_dir: PosixPath):
		print("Checking groups directory...")
		file_total = 0
		for _, _, files in groups_dir.walk():
			file_total += len(files)

		progress = tqdm(desc="Reading groups", unit="file", total=file_total)
		for dirpath, _, files in groups_dir.walk():
			for file in files:
				match file:
					case ".scraped":
						pass
					case "group-names":
						cls.read_groups(dirpath / file)
					case ".folders":
						cls.read_folders(dirpath / file)
					case ".names":
						cls.read_names(dirpath / file)
					case _:
						cls.read_stories(dirpath / file)
				progress.update()
		progress.close()

	@orm.db_session
	def update_last_checked(self):
		print("Updating group last checked...")
		most_recent = datetime.fromtimestamp(0, UTC)
		for group in tqdm(Group.select(lambda g: g.id and not g.folders.is_empty()),
							"Calculating", unit="g", bar_format="{l_bar}{bar}"):
			last_checked_str = max(f.last_checked for f in group.folders)
			group.last_checked = datetime.fromisoformat(last_checked_str)
			if group.last_checked > most_recent:
				most_recent = group.last_checked
		KV.set("last checked", most_recent)
		KV.set("ready", True)
		self.ready = True
		last_checked_local = most_recent.astimezone().isoformat(timespec="minutes")
		print(f"Last checked: {last_checked_local}")

	def scan_all(self, groups_dir: PosixPath):
		# split into separate DB sessions because the first modifies the fields that the latter reads
		self.scan_directory(groups_dir)
		# it can also be fixed by setting volatile=true on Folder.last_checked
		self.update_last_checked()
		# has to be run outside a transaction, but PonyORM does not allow anything except select
		KV._database_.provider.pool.con.execute("vacuum")

	@staticmethod
	def read_groups(group_list: PosixPath):
		# groupid "group name"
		with group_list.open(newline="") as fh:
			reader = DictReader(fh, ["group", "name"], delimiter=" ")
			for row in reader:
				group_id = int(row["group"])
				try:
					group = Group[group_id]
				except orm.ObjectNotFound:
					group = Group(id=group_id)
				group.name = unescape(row["name"])

	@staticmethod
	def read_folders(folder_list: PosixPath):
		# groupid folderid parentid
		with folder_list.open(newline="") as fh:
			reader = DictReader(fh, ["group", "folder", "parent"], delimiter=" ")
			for row in reader:
				group_id = int(row["group"])
				folder_id = int(row["folder"])
				try:
					group = Group[group_id]
				except orm.ObjectNotFound:
					group = Group(id=group_id)
				try:
					folder = Folder[folder_id]
					folder.group = int(row["group"])
				except orm.ObjectNotFound:
					folder = Folder(id=folder_id, group=group)
				parent = int(row["parent"])
				if parent:
					folder.parent = parent

	@staticmethod
	def read_names(folder_list: PosixPath):
		# folderid "foldername"
		with folder_list.open(newline="") as fh:
			reader = DictReader(fh, ["folder", "name"], delimiter=" ")
			for row in reader:
				folder_id = int(row["folder"])
				try:
					folder = Folder[folder_id]
					folder.name = unescape(row["name"])
				except orm.ObjectNotFound:
					folder = Folder(id=folder_id, name=unescape(row["name"]))

	@staticmethod
	def read_stories(story_list: PosixPath):
		text = story_list.read_text()
		obj_id = int(story_list.name)
		last_checked = datetime.fromtimestamp(story_list.stat().st_mtime, UTC)
		stories = [int(story_id) for story_id in text.splitlines()]
		try:
			obj = Folder[obj_id]
			obj.last_checked = last_checked
		except orm.ObjectNotFound:
			obj = Folder(id=obj_id)
		obj.last_checked = last_checked
		# obj.stories = stories
		orm.delete(p for p in Placement if p.folder == obj)
		for story_id in stories:
			Placement(folder=obj, story=story_id)

	@staticmethod
	def tree(folders: list[Folder]) -> list[Folder]:
		# recursively walk up a folder tree until the top is found
		if folders[0].parent:
			try:
				return [Folder[folders[0].parent], *folders]
			except orm.ObjectNotFound:
				#this shouldn't  happen, but could in case of orphan folders on future updates to the database
				stub_folder = Folder[0]
				stub_folder.group = folders[0].group
				return [stub_folder, *folders]
		else:
			return folders

	@orm.db_session
	def groups4story(self, story: int, parents: bool = True) -> GroupInfo:
		"""
		Gather groups for a story, as well as folders and their paths. Provided as IDs and string paths.
		:param story: FiMFic story ID
		:param parents: folder_ids will include all parent folders, rather than just the leaf containing the story
		:return: GroupInfo
		"""
		if not self.ready:
			raise ValueError("The database has not been populated yet!")
		# found = Folder.select(lambda a: story in a.stories)
		# found = Placement.select(lambda m: m.story == story)
		found = Placement.select(story=story)
		group_ids = set()
		folder_ids = set()
		group_names = set()
		paths = []
		for f in found:
			folder_path = self.tree([f.folder])
			group = folder_path[0].group
			group_name = group.name
			if group_name:
				paths.append(f"{group_name}/" + "/".join([b.name for b in folder_path]))
				group_names.add(group_name)
			group_ids.add(group.id)
			if parents:
				folder_ids |= {b.id for b in folder_path if b.id}
			else:
				folder_ids.add(f.folder.group.id)
		return GroupInfo(story, group_names, group_ids, folder_ids, paths)

	@orm.db_session
	def all_story_groups(self, parents: bool = True):
		if not self.ready:
			raise ValueError("The database has not been populated yet!")
		#found = Placement.select(lambda p: p in Placement)
		# obtuse ORM nonsense; this way is much, much, much faster
		found = orm.select((p.story, ) for p in Placement) # it automatically does distinct
		for story in found:
			yield self.groups4story(story, parents)


if __name__ == "__main__":
	me = PosixPath(__file__)
	db_path = me.with_name("folders.sqlite")
	configuration = ArgumentParser("Import flat files group information from https://github.com/uis246/fimfarc-search/ to a database!")
	configuration.add_argument("--db-path", help="path to output sqlite database", default=str(db_path))
	configuration.add_argument("--folder-path", help="path to extracted groups archive", required=False)
	args = configuration.parse_args()
	group_db = GroupMeta(args.db_path)
	if not group_db.ready:
		folder_path = PosixPath(args.folder_path)
		assert folder_path.exists(), "You must specify a real folder path to populate the database!"
		group_db.scan_all(folder_path)
	from time import process_time
	start = process_time()
	for story_meta in group_db.all_story_groups():
		continue
	end = process_time()
	print(f"Scanned all stories in {end - start} seconds")
