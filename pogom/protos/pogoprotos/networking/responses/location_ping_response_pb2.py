# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: pogoprotos/networking/responses/location_ping_response.proto

import sys
_b=sys.version_info[0]<3 and (lambda x:x) or (lambda x:x.encode('latin1'))
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()




DESCRIPTOR = _descriptor.FileDescriptor(
  name='pogoprotos/networking/responses/location_ping_response.proto',
  package='pogoprotos.networking.responses',
  syntax='proto3',
  serialized_options=None,
  serialized_pb=_b('\n<pogoprotos/networking/responses/location_ping_response.proto\x12\x1fpogoprotos.networking.responses\"\x16\n\x14LocationPingResponseb\x06proto3')
)




_LOCATIONPINGRESPONSE = _descriptor.Descriptor(
  name='LocationPingResponse',
  full_name='pogoprotos.networking.responses.LocationPingResponse',
  filename=None,
  file=DESCRIPTOR,
  containing_type=None,
  fields=[
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
  serialized_start=97,
  serialized_end=119,
)

DESCRIPTOR.message_types_by_name['LocationPingResponse'] = _LOCATIONPINGRESPONSE
_sym_db.RegisterFileDescriptor(DESCRIPTOR)

LocationPingResponse = _reflection.GeneratedProtocolMessageType('LocationPingResponse', (_message.Message,), dict(
  DESCRIPTOR = _LOCATIONPINGRESPONSE,
  __module__ = 'pogoprotos.networking.responses.location_ping_response_pb2'
  # @@protoc_insertion_point(class_scope:pogoprotos.networking.responses.LocationPingResponse)
  ))
_sym_db.RegisterMessage(LocationPingResponse)


# @@protoc_insertion_point(module_scope)
