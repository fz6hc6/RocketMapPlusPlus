# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: pogoprotos/data/friends/friendship_level_data.proto

import sys
_b=sys.version_info[0]<3 and (lambda x:x) or (lambda x:x.encode('latin1'))
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from pogoprotos.enums import friendship_level_milestone_pb2 as pogoprotos_dot_enums_dot_friendship__level__milestone__pb2


DESCRIPTOR = _descriptor.FileDescriptor(
  name='pogoprotos/data/friends/friendship_level_data.proto',
  package='pogoprotos.data.friends',
  syntax='proto3',
  serialized_options=None,
  serialized_pb=_b('\n3pogoprotos/data/friends/friendship_level_data.proto\x12\x17pogoprotos.data.friends\x1a\x31pogoprotos/enums/friendship_level_milestone.proto\"\xc3\x02\n\x13\x46riendshipLevelData\x12\x0e\n\x06\x62ucket\x18\x01 \x01(\x03\x12\x1b\n\x13points_earned_today\x18\x02 \x01(\x05\x12P\n\x1c\x61warded_friendship_milestone\x18\x03 \x01(\x0e\x32*.pogoprotos.enums.FriendshipLevelMilestone\x12P\n\x1c\x63urrent_friendship_milestone\x18\x04 \x01(\x0e\x32*.pogoprotos.enums.FriendshipLevelMilestone\x12\x35\n-next_friendship_milestone_progress_percentage\x18\x05 \x01(\x01\x12$\n\x1cpoints_toward_next_milestone\x18\x06 \x01(\x05\x62\x06proto3')
  ,
  dependencies=[pogoprotos_dot_enums_dot_friendship__level__milestone__pb2.DESCRIPTOR,])




_FRIENDSHIPLEVELDATA = _descriptor.Descriptor(
  name='FriendshipLevelData',
  full_name='pogoprotos.data.friends.FriendshipLevelData',
  filename=None,
  file=DESCRIPTOR,
  containing_type=None,
  fields=[
    _descriptor.FieldDescriptor(
      name='bucket', full_name='pogoprotos.data.friends.FriendshipLevelData.bucket', index=0,
      number=1, type=3, cpp_type=2, label=1,
      has_default_value=False, default_value=0,
      message_type=None, enum_type=None, containing_type=None,
      is_extension=False, extension_scope=None,
      serialized_options=None, file=DESCRIPTOR),
    _descriptor.FieldDescriptor(
      name='points_earned_today', full_name='pogoprotos.data.friends.FriendshipLevelData.points_earned_today', index=1,
      number=2, type=5, cpp_type=1, label=1,
      has_default_value=False, default_value=0,
      message_type=None, enum_type=None, containing_type=None,
      is_extension=False, extension_scope=None,
      serialized_options=None, file=DESCRIPTOR),
    _descriptor.FieldDescriptor(
      name='awarded_friendship_milestone', full_name='pogoprotos.data.friends.FriendshipLevelData.awarded_friendship_milestone', index=2,
      number=3, type=14, cpp_type=8, label=1,
      has_default_value=False, default_value=0,
      message_type=None, enum_type=None, containing_type=None,
      is_extension=False, extension_scope=None,
      serialized_options=None, file=DESCRIPTOR),
    _descriptor.FieldDescriptor(
      name='current_friendship_milestone', full_name='pogoprotos.data.friends.FriendshipLevelData.current_friendship_milestone', index=3,
      number=4, type=14, cpp_type=8, label=1,
      has_default_value=False, default_value=0,
      message_type=None, enum_type=None, containing_type=None,
      is_extension=False, extension_scope=None,
      serialized_options=None, file=DESCRIPTOR),
    _descriptor.FieldDescriptor(
      name='next_friendship_milestone_progress_percentage', full_name='pogoprotos.data.friends.FriendshipLevelData.next_friendship_milestone_progress_percentage', index=4,
      number=5, type=1, cpp_type=5, label=1,
      has_default_value=False, default_value=float(0),
      message_type=None, enum_type=None, containing_type=None,
      is_extension=False, extension_scope=None,
      serialized_options=None, file=DESCRIPTOR),
    _descriptor.FieldDescriptor(
      name='points_toward_next_milestone', full_name='pogoprotos.data.friends.FriendshipLevelData.points_toward_next_milestone', index=5,
      number=6, type=5, cpp_type=1, label=1,
      has_default_value=False, default_value=0,
      message_type=None, enum_type=None, containing_type=None,
      is_extension=False, extension_scope=None,
      serialized_options=None, file=DESCRIPTOR),
  ],
  extensions=[
  ],
  nested_types=[],
  enum_types=[
  ],
  serialized_options=None,
  is_extendable=False,
  syntax='proto3',
  extension_ranges=[],
  oneofs=[
  ],
  serialized_start=132,
  serialized_end=455,
)

_FRIENDSHIPLEVELDATA.fields_by_name['awarded_friendship_milestone'].enum_type = pogoprotos_dot_enums_dot_friendship__level__milestone__pb2._FRIENDSHIPLEVELMILESTONE
_FRIENDSHIPLEVELDATA.fields_by_name['current_friendship_milestone'].enum_type = pogoprotos_dot_enums_dot_friendship__level__milestone__pb2._FRIENDSHIPLEVELMILESTONE
DESCRIPTOR.message_types_by_name['FriendshipLevelData'] = _FRIENDSHIPLEVELDATA
_sym_db.RegisterFileDescriptor(DESCRIPTOR)

FriendshipLevelData = _reflection.GeneratedProtocolMessageType('FriendshipLevelData', (_message.Message,), dict(
  DESCRIPTOR = _FRIENDSHIPLEVELDATA,
  __module__ = 'pogoprotos.data.friends.friendship_level_data_pb2'
  # @@protoc_insertion_point(class_scope:pogoprotos.data.friends.FriendshipLevelData)
  ))
_sym_db.RegisterMessage(FriendshipLevelData)


# @@protoc_insertion_point(module_scope)