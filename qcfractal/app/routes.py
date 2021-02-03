"""
Routes handlers for Flask
"""

from qcelemental.util import deserialize, serialize
from ..storage_sockets.storage_utils import add_metadata_template
from ..interface.models.rest_models import rest_model
from ..interface.models.task_models import PriorityEnum, TaskStatusEnum
from ..interface.models.records import RecordStatusEnum
from ..interface.models.model_builder import build_procedure
from ..procedures import check_procedure_available, get_procedure_parser
from ..services import initialize_service
from ..extras import get_information as get_qcfractal_information

from flask import jsonify, request, make_response
import traceback
import collections
from flask_jwt_extended import (
    fresh_jwt_required,
    create_access_token,
    get_jwt_claims,
    jwt_refresh_token_required,
    create_refresh_token,
    get_jwt_identity,
    verify_jwt_in_request,
    verify_jwt_in_request_optional,
)
from urllib.parse import urlparse
from ..policyuniverse import Policy
from flask import Blueprint, current_app, session, Response
from functools import wraps
from werkzeug.exceptions import BadRequest, NotFound, Forbidden, Unauthorized

from . import api_logger, storage_socket, view_handler


main = Blueprint("main", __name__)


_valid_encodings = {
    "application/json": "json",
    "application/json-ext": "json-ext",
    "application/msgpack-ext": "msgpack-ext",
}

# TODO: not implemented yet
_logging_param_counts = {"id"}
_read_permissions = {}

def check_access(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        """
        Call the route (fn) if allowed to access the url using the given
        permissions in the JWT token in the request headers

        1- If no security (JWT_ENABLED=False), always allow
        2- If JWT_ENABLED:
            if read allowed (allow_read=True), use the default read permissions
            otherwise, check against the logged-in user permissions
            from the headers' JWT token
        """

        current_app.logger.debug(f"JWT_ENABLED: {current_app.config.JWT_ENABLED}")
        current_app.logger.debug(f"ALLOW_UNAUTHENTICATED_READ: {current_app.config.ALLOW_UNAUTHENTICATED_READ}")

        # if no auth required, always allowed
        if not current_app.config.JWT_ENABLED:
            return fn(*args, **kwargs)

        # load read permissions from DB if not read
        global _read_permissions
        if not _read_permissions:
            _, _read_permissions = storage_socket.get_role("read")
            _read_permissions = _read_permissions["permissions"]

        # if read is allowed without login, use read_permissions
        # otherwise, check logged-in permissions
        if current_app.config.ALLOW_UNAUTHENTICATED_READ:
            # don't raise exception if no JWT is found
            verify_jwt_in_request_optional()
        else:
            # read JWT token from request headers
            verify_jwt_in_request()

        claims = get_jwt_claims()
        permissions = claims.get("permissions", {})

        try:
            # host_url = request.host_url
            identity = get_jwt_identity() or 'anonymous'
            resource = urlparse(request.url).path.split("/")[1]
            context = {
                "Principal": identity,
                "Action": request.method,
                "Resource": resource
                # "IpAddress": request.remote_addr,
                # "AccessTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            current_app.logger.info(f"Permissions: {permissions}")
            current_app.logger.info(f"Context: {context}")
            policy = Policy(permissions)
            if not policy.evaluate(context):
                if not Policy(_read_permissions).evaluate(context):
                    return Forbidden(f"User {identity} is not authorized to access '{resource}' resource.")

        except Exception as e:
            current_app.logger.info("Error in evaluating JWT permissions: \n" + str(e))
            return BadRequest("Error in evaluating JWT permissions")

        return fn(*args, **kwargs)

    return wrapper


def parse_bodymodel(model):
    """Parse request body using pydantic models"""

    try:
        return model(**request.data)
    except Exception as e:
        current_app.logger.error("Invalid request body:\n" + str(e))
        raise BadRequest("Invalid body: " + str(e))


class SerializedResponse(Response):
    """Serialize pydantic response using the given encoding and pass it
    as a flask response object"""

    def __init__(self, response, **kwargs):

        # TODO: support other content types? We would need to check the Accept header
        content_type = 'application/msgpack-ext'
        encoding = _valid_encodings[content_type]
        response = serialize(response, encoding)
        super(SerializedResponse, self).__init__(response, content_type=content_type, **kwargs)

@main.before_request
def before_request_func():
    ###############################################################
    # Deserialize the various encodings we support (like msgpack) #
    ###############################################################

    try:
        # default to "application/json"
        session["content_type"] = request.headers.get("Content-Type", "application/json")
        session["encoding"] = _valid_encodings[session["content_type"]]
    except KeyError as e:
        raise BadRequest(f"Did not understand 'Content-Type'. {e}")

    try:
        # Check to see if we have a json that is encoded as bytes rather than a string
        if (session["encoding"] == "json") and isinstance(request.data, bytes):
            blob = request.data.decode()
        else:
            blob = request.data

        if blob:
            request.data = deserialize(blob, session["encoding"])
        else:
            request.data = None
    except Exception as e:
        raise BadRequest(f"Could not deserialize body. {e}")


#@main.after_request
#def after_request_func(response):
#
#    # Always reply in the format sent
#    response.headers["Content-Type"] = session["content_type"]
#
#    exclude_uris = ["/task_queue", "/service_queue", "/queue_manager"]
#
#    # No associated data, so skip all of this
#    # (maybe caused by not using portal or not using the REST API correctly?)
#    if request.data is None:
#        return response
#
#    if api_current_app.logger.enabled and request.method == "GET" and request.path not in exclude_uris:
#
#        extra_params = request.data.copy()
#        if _logging_param_counts:
#            for key in _logging_param_counts:
#                if "data" in extra_params and extra_params["data"].get(key, None):
#                    extra_params["data"][key] = len(extra_params["data"][key])
#
#        if "data" in extra_params:
#            extra_params["data"] = {k: v for k, v in extra_params["data"].items() if v is not None}
#
#        extra_params = json.dumps(extra_params)
#
#        log = api_current_app.logger.get_api_access_log(request=request, extra_params=extra_params)
#        storage_socket.save_access(log)
#
#        # current_app.logger.info('Done saving API access to the database')
#
#    return response


@main.errorhandler(KeyError)
def handle_python_errors(error):
    return jsonify(msg=str(error)), 400
    # return BadRequest(str(error))


@main.errorhandler(BadRequest)
def handle_invalid_usage(error):
    return jsonify(msg=str(error)), error.code


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#                            Routes
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@main.route("/register", methods=["POST"])
def register():
    if request.is_json:
        username = request.json["username"]
        password = request.json["password"]
    else:
        username = request.form["username"]
        password = request.form["password"]

    success = storage_socket.add_user(username, password=password, rolename="user")
    if success:
        return jsonify(msg="New user created!"), 201
    else:
        current_app.logger.info("\n>>> Failed to add user. Perhaps the username is already taken?")
        return jsonify(msg="Failed to add user."), 500


@main.route("/login", methods=["POST"])
def login():
    if request.is_json:
        username = request.json["username"]
        password = request.json["password"]
    else:
        username = request.form["username"]
        password = request.form["password"]

    success, error_message, permissions = storage_socket.verify_user(username, password)
    if success:
        access_token = create_access_token(identity=username, user_claims={"permissions": permissions})
        # expires_delta=datetime.timedelta(days=3))
        refresh_token = create_refresh_token(identity=username)
        return jsonify(msg="Login succeeded!", access_token=access_token, refresh_token=refresh_token), 200
    else:
        return Unauthorized(error_message)


@main.route("/information", methods=["GET"])
def get_information():
    import logging
    qcf_cfg = current_app.config["QCFRACTAL_CONFIG"]

    db_data = storage_socket.get_server_stats_log(limit=1)["data"]
    public_info = {
        'name': qcf_cfg.name,
        'manager_heartbeat_frequency': qcf_cfg.heartbeat_frequency,
        'version': get_qcfractal_information("version"),
        'query_limit': storage_socket.get_limit('default'), # TODO get all limits
        "client_lower_version_limit": "0.14.0",  # Must be XX.YY.ZZ
        "client_upper_version_limit": "0.15.99",  # Must be XX.YY.ZZ
        "collection": 0,
        "molecule": 0,
        "result": 0,
        "kvstore": 0,
        "last_update": None
    }

    if len(db_data) > 0:
        counts = {
            "collection": db_data[0].get("collection_count", 0),
            "molecule": db_data[0].get("molecule_count", 0),
            "result": db_data[0].get("result_count", 0),
            "kvstore": db_data[0].get("kvstore_count", 0),
            "last_update": db_data[0].get("timestamp", None)
        }
        public_info.update(counts)

    return SerializedResponse(public_info)


@main.route("/refresh", methods=["POST"])
@jwt_refresh_token_required
def refresh():
    username = get_jwt_identity()
    ret = {
        "access_token": create_access_token(
            identity=username, user_claims={"permissions": storage_socket.get_user_permissions(username)}
        )
    }
    return jsonify(ret), 200


@main.route("/fresh-login", methods=["POST"])
def fresh_login():
    if request.is_json:
        username = request.json["username"]
        password = request.json["password"]
    else:
        username = request.form["username"]
        password = request.form["password"]

    success, error_message, permissions = storage_socket.verify_user(username, password)
    if success:
        access_token = create_access_token(identity=username, user_claims={"permissions": permissions}, fresh=True)
        return jsonify(msg="Fresh login succeeded!", access_token=access_token), 200
    else:
        return Unauthorized(error_message)


@main.route("/molecule", methods=["GET"])
@check_access
def get_molecule():
    """
    Request:
        "meta" - Overall options to the Molecule pull request
            - "index" - What kind of index used to find the data ("id", "molecule_hash", "molecular_formula")
        "data" - A dictionary of {key : index} requests

    Returns:
        "meta" - Metadata associated with the query
            - "errors" - A list of errors in (index, error_id) format.
            - "n_found" - The number of molecule found.
            - "success" - If the query was successful or not.
            - "error_description" - A string based description of the error or False
            - "missing" - A list of keys that were not found.
        "data" - A dictionary of {key : molecule JSON} results
    """

    body_model, response_model = rest_model("molecule", "get")
    body = parse_bodymodel(body_model)
    molecules = storage_socket.get_molecules(**{**body.data.dict(), **body.meta.dict()})
    response = response_model(**molecules)

    return SerializedResponse(response)


@main.route("/molecule", methods=["POST"])
@check_access
def post_molecule():
    """
    Request:
        "meta" - Overall options to the Molecule pull request
            - No current options
        "data" - A dictionary of {key : molecule JSON} requests

    Returns:
        "meta" - Metadata associated with the query
            - "errors" - A list of errors in (index, error_id) format.
            - "n_inserted" - The number of molecule inserted.
            - "success" - If the query was successful or not.
            - "error_description" - A string based description of the error or False
            - "duplicates" - A list of keys that were already inserted.
        "data" - A dictionary of {key : id} results
    """

    body_model, response_model = rest_model("molecule", "post")
    body = parse_bodymodel(body_model)

    ret = storage_socket.add_molecules(body.data)
    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/kvstore", methods=["GET"])
@check_access
def get_kvstore():
    """
    Request:
        "data" - A list of key requests
    Returns:
        "meta" - Metadata associated with the query
            - "errors" - A list of errors in (index, error_id) format.
            - "n_found" - The number of molecule found.
            - "success" - If the query was successful or not.
            - "error_description" - A string based description of the error or False
            - "missing" - A list of keys that were not found.
        "data" - A dictionary of {key : value} dictionary of the results
    """

    body_model, response_model = rest_model("kvstore", "get")
    body = parse_bodymodel(body_model)

    ret = storage_socket.get_kvstore(body.data.id)
    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/keyword", methods=["GET"])
@check_access
def get_keyword():
    body_model, response_model = rest_model("keyword", "get")
    body = parse_bodymodel(body_model)

    ret = storage_socket.get_keywords(**{**body.data.dict(), **body.meta.dict()}, with_ids=False)
    response = response_model(**ret)

    current_app.logger.info("GET: Keywords - {} pulls.".format(len(response.data)))
    return SerializedResponse(response)


@main.route("/keyword", methods=["POST"])
@check_access
def post_keyword():

    body_model, response_model = rest_model("keyword", "post")
    body = parse_bodymodel(body_model)

    ret = storage_socket.add_keywords(body.data)
    response = response_model(**ret)

    current_app.logger.info("POST: Keywords - {} inserted.".format(response.meta.n_inserted))
    return SerializedResponse(response)


@main.route("/collection", methods=["GET"])
@main.route("/collection/<int:collection_id>", methods=["GET"])
@main.route("/collection/<int:collection_id>/<string:view_function>", methods=["GET"])
@check_access
def get_collection(collection_id: int = None, view_function: str = None):
    # List collections

    view_function_vals = ("value", "entry", "list", "molecule")
    if view_function is not None and view_function not in view_function_vals:
        raise NotFound(f"URL Not Found. view_function must be in : {view_function_vals}")

    if (collection_id is None) and (view_function is None):
        body_model, response_model = rest_model("collection", "get")
        body = parse_bodymodel(body_model)

        cols = storage_socket.get_collections(
            **body.data.dict(), include=body.meta.include, exclude=body.meta.exclude
        )
        response = response_model(**cols)

    # Get specific collection
    elif (collection_id is not None) and (view_function is None):
        body_model, response_model = rest_model("collection", "get")

        body = parse_bodymodel(body_model)
        cols = storage_socket.get_collections(
            **body.data.dict(), col_id=int(collection_id), include=body.meta.include, exclude=body.meta.exclude
        )
        response = response_model(**cols)

    # View-backed function on collection
    elif (collection_id is not None) and (view_function is not None):
        body_model, response_model = rest_model(f"collection/{collection_id}/{view_function}", "get")
        body = parse_bodymodel(body_model)
        if view_handler is None:
            meta = {
                "success": False,
                "error_description": "Server does not support collection views.",
                "errors": [],
                "msgpacked_cols": [],
            }
            response = response_model(meta=meta, data=None)
            return SerializedResponse(response)

        result = view_handler.handle_request(collection_id, view_function, body.data.dict())
        response = response_model(**result)

    # Unreachable?
    else:
        body_model, response_model = rest_model("collection", "get")
        meta = add_metadata_template()
        meta["success"] = False
        meta["error_description"] = "GET request for view with no collection ID not understood."
        response = response_model(meta=meta, data=None)

    return SerializedResponse(response)


@main.route("/collection", methods=["POST"])
@main.route("/collection/<int:collection_id>", methods=["POST"])
@main.route("/collection/<int:collection_id>/<string:view_function>", methods=["POST"])
@check_access
def post_collection(collection_id: int = None, view_function: str = None):

    view_function_vals = ("value", "entry", "list", "molecule")
    if view_function is not None and view_function not in view_function_vals:
        raise NotFound(f"URL Not Found. view_function must be in : {view_function_vals}")

    body_model, response_model = rest_model("collection", "post")
    body = parse_bodymodel(body_model)

    # POST requests not supported for anything other than "/collection"
    if collection_id is not None or view_function is not None:
        meta = add_metadata_template()
        meta["success"] = False
        meta["error_description"] = "POST requests not supported for sub-resources of /collection"
        response = response_model(meta=meta, data=None)

        return SerializedResponse(response)

    ret = storage_socket.add_collection(body.data.dict(), overwrite=body.meta.overwrite)
    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/collection", methods=["DELETE"])
@main.route("/collection/<int:collection_id>", methods=["DELETE"])
@check_access
def delete_collection(collection_id: int, view_function: str):
    body_model, response_model = rest_model(f"collection/{collection_id}", "delete")
    ret = storage_socket.del_collection(col_id=collection_id)
    if ret == 0:
        return jsonify(msg="Collection does not exist."), 404
    else:
        response = response_model(meta={"success": True, "errors": [], "error_description": False})

    return SerializedResponse(response)


@main.route("/result", methods=["GET"])
@check_access
def get_result():

    body_model, response_model = rest_model("result", "get")
    body = parse_bodymodel(body_model)

    ret = storage_socket.get_results(**{**body.data.dict(), **body.meta.dict()})
    response = response_model(**ret)

    current_app.logger.info("GET: Results - {} pulls.".format(len(response.data)))

    return SerializedResponse(response)


@main.route("/wavefunctionstore", methods=["GET"])
@check_access
def get_wave_function():

    body_model, response_model = rest_model("wavefunctionstore", "get")
    body = parse_bodymodel(body_model)

    ret = storage_socket.get_wavefunction_store(body.data.id, include=body.meta.include)
    if len(ret["data"]):
        ret["data"] = ret["data"][0]
    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/procedure", methods=["GET"])
@main.route("/procedure/<string:query_type>", methods=["GET"])
@check_access
def get_procedure(query_type: str = "get"):
    body_model, response_model = rest_model("procedure", query_type)
    body = parse_bodymodel(body_model)

    # try:
    if query_type == "get":
        ret = storage_socket.get_procedures(**{**body.data.dict(), **body.meta.dict()})
    else:  # all other queries, like 'best_opt_results'
        ret = storage_socket.custom_query(
            "procedure", query_type, **{**body.data.dict(), **body.meta.dict()}
        )
    # except KeyError as e:
    #     return jsonify(msg=str(e)), 500

    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/optimization/<string:query_type>", methods=["GET"])
@check_access
def get_optimization(query_type: str):
    body_model, response_model = rest_model(f"optimization/{query_type}", "get")
    body = parse_bodymodel(body_model)

    # try:
    if query_type == "get":
        ret = storage_socket.get_procedures(**{**body.data.dict(), **body.meta.dict()})
    else:  # all other queries, like 'best_opt_results'
        ret = storage_socket.custom_query(
            "optimization", query_type, **{**body.data.dict(), **body.meta.dict()}
        )
    # except KeyError as e:
    #     return jsonify(msg=str(e)), 500

    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/task_queue", methods=["GET"])
@check_access
def get_task_queue():
    body_model, response_model = rest_model("task_queue", "get")
    body = parse_bodymodel(body_model)

    tasks = storage_socket.get_queue(**{**body.data.dict(), **body.meta.dict()})
    response = response_model(**tasks)

    return SerializedResponse(response)


@main.route("/task_queue", methods=["POST"])
@check_access
def post_task_queue():
    body_model, response_model = rest_model("task_queue", "post")
    body = parse_bodymodel(body_model)

    # Format and submit tasks
    if not check_procedure_available(body.meta.procedure):
        return jsonify(msg="Unknown procedure {}.".format(body.meta.procedure)), 400

    procedure_parser = get_procedure_parser(body.meta.procedure, storage_socket)

    # Verify the procedure
    verify = procedure_parser.verify_input(body)
    if verify is not True:
        return jsonify(msg="Verify error"), 400

    payload = procedure_parser.submit_tasks(body)
    response = response_model(**payload)

    return SerializedResponse(response)


@main.route("/task_queue", methods=["PUT"])
@check_access
def put_task_queue():
    """Modifies tasks in the task queue"""

    body_model, response_model = rest_model("task_queue", "put")
    body = parse_bodymodel(body_model)

    if (body.data.id is None) and (body.data.base_result is None):
        return jsonify(msg="Id or ResultId must be specified."), 400

    if body.meta.operation == "restart":
        d = body.data.dict()
        d.pop("new_tag", None)
        d.pop("new_priority", None)
        tasks_updated = storage_socket.queue_reset_status(**d, reset_error=True)
        data = {"n_updated": tasks_updated}
    elif body.meta.operation == "regenerate":
        tasks_updated = 0
        result_data = storage_socket.get_procedures(id=body.data.base_result)["data"]

        new_tag = body.data.new_tag
        if body.data.new_priority is None:
            new_priority = PriorityEnum.NORMAL
        else:
            new_priority = PriorityEnum(int(body.data.new_priority))

        for r in result_data:
            model = build_procedure(r)

            # Only regenerate the task if the base record is not complete
            # This will not do anything if the task already exists
            if model.status != RecordStatusEnum.complete:
                procedure_parser = get_procedure_parser(model.procedure, storage_socket)

                task_info = procedure_parser.create_tasks([model], tag=new_tag, priority=new_priority)
                n_inserted = task_info["meta"]["n_inserted"]
                tasks_updated += n_inserted

                # If we inserted a new task, then also reset base result statuses
                # (ie, if it was running, then it obviously isn't since we made a new task)
                if n_inserted > 0:
                    storage_socket.reset_base_result_status(id=body.data.base_result)

            data = {"n_updated": tasks_updated}
    elif body.meta.operation == "modify":
        tasks_updated = storage_socket.queue_modify_tasks(
            id=body.data.id,
            base_result=body.data.base_result,
            new_tag=body.data.new_tag,
            new_priority=body.data.new_priority,
        )
        data = {"n_updated": tasks_updated}
    else:
        return jsonify(msg=f"Operation '{body.meta.operation}' is not valid."), 400

    response = response_model(data=data, meta={"errors": [], "success": True, "error_description": False})

    current_app.logger.info(f"PUT: TaskQueue - Operation: {body.meta.operation} - {tasks_updated}.")

    return SerializedResponse(response)


@main.route("/service_queue", methods=["GET"])
@check_access
def get_service_queue():
    body_model, response_model = rest_model("service_queue", "get")
    body = parse_bodymodel(body_model)

    ret = storage_socket.get_services(**{**body.data.dict(), **body.meta.dict()})
    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/service_queue", methods=["POST"])
@check_access
def post_service_queue():
    """Posts new services to the service queue."""

    body_model, response_model = rest_model("service_queue", "post")
    body = parse_bodymodel(body_model)

    new_services = []
    for service_input in body.data:
        # Get molecules with ids
        if isinstance(service_input.initial_molecule, list):
            molecules = storage_socket.get_add_molecules_mixed(service_input.initial_molecule)["data"]
            if len(molecules) != len(service_input.initial_molecule):
                raise KeyError("We should catch this error.")
        else:
            molecules = storage_socket.get_add_molecules_mixed([service_input.initial_molecule])["data"][0]

        # Update the input and build a service object
        service_input = service_input.copy(update={"initial_molecule": molecules})
        new_services.append(
            initialize_service(
                storage_socket,
                service_input,
                tag=body.meta.tag,
                priority=body.meta.priority,
            )
        )

    ret = storage_socket.add_services(new_services)
    ret["data"] = {"ids": ret["data"], "existing": ret["meta"]["duplicates"]}
    ret["data"]["submitted"] = list(set(ret["data"]["ids"]) - set(ret["meta"]["duplicates"]))
    response = response_model(**ret)

    return SerializedResponse(response)


@main.route("/service_queue", methods=["PUT"])
@check_access
def put_service_queue():
    """Modifies services in the service queue"""

    body_model, response_model = rest_model("service_queue", "put")
    body = parse_bodymodel(body_model)

    if (body.data.id is None) and (body.data.procedure_id is None):
        return jsonify(msg="Id or ProcedureId must be specified."), 400

    if body.meta.operation == "restart":
        updates = storage_socket.update_service_status("running", **body.data.dict())
        data = {"n_updated": updates}
    else:
        return jsonify(msg="Operation '{operation}' is not valid."), 400

    response = response_model(data=data, meta={"errors": [], "success": True, "error_description": False})

    return SerializedResponse(response)


def _get_name_from_metadata(meta):
    """
    Form the canonical name string.
    """
    ret = meta.cluster + "-" + meta.hostname + "-" + meta.uuid
    return ret

def _insert_complete_tasks(storage_socket, body):

        results = body.data
        meta = body.meta
        task_ids = list(results.keys())

        manager_name = _get_name_from_metadata(meta)
        current_app.logger.info("QueueManager: Received completed tasks from {}.".format(manager_name))
        current_app.logger.info("              Task ids: " + " ".join(task_ids))

        # Pivot data so that we group all results in categories
        queue = storage_socket.get_queue(id=task_ids)["data"]
        queue = {v.id: v for v in queue}

        task_success = 0
        task_failures = 0
        task_totals = len(results.items())

        failure_parser = get_procedure_parser('failed_operation', storage_socket)

        for task_id, result in results.items():
            existing_task_data = queue.get(task_id, None)

            # Does the task exist?
            if existing_task_data is None:
                current_app.logger.warning(f"Task id {task_id} does not exist in the task queue.")
                task_failures += 1
                continue

            try:
                #################################################################
                # Perform some checks for consistency
                #################################################################
                # Information passed to handle_completed_output for the various output parsers
                task_info = {
                    "result": result,
                    "task_id": task_id,
                    "base_result_id": existing_task_data.base_result,
                    "manager_name": manager_name,
                }

                # Is the task in the running state
                # If so, do not attempt to modify the task queue. Just move on
                if existing_task_data.status != TaskStatusEnum.running:
                    current_app.logger.warning(f"Task id {task_id} is not in the running state.")
                    task_failures += 1

                # Was the manager that sent the data the one that was assigned?
                # If so, do not attempt to modify the task queue. Just move on
                elif existing_task_data.manager != manager_name:
                    current_app.logger.warning(f"Task id {task_id} belongs to {existing_task_data.manager}, not this manager")
                    task_failures += 1

                # Failed task returning FailedOperation
                # TODO - better detection of FailedOperation. Right now, the easiest way to detect
                #        FailedOperation is to see if 'input_data' is part of it. Other results don't have that
                elif result["success"] is False and "input_data" in result:
                    failure_parser.handle_completed_output([task_info])
                    task_failures += 1

                elif result["success"] is not True:
                    # QCEngine should always return either FailedOperation, or some result with success == True
                    current_app.logger.warning(f"Task id {task_id} returned success != True, but is not a FailedOperation")
                    task_failures += 1

                # Manager returned a full, successful result
                else:
                    parser = get_procedure_parser(queue[task_id].parser, storage_socket)
                    parser.handle_completed_output([task_info])
                    task_success += 1

            except Exception:
                msg = "Internal FractalServer Error:\n" + traceback.format_exc()
                error = {"error_type": "internal_fractal_error", "error_message": msg}
                failed_op = {"error": error, "success": False}

                fail_info = {
                    "result": failed_op,
                    "task_id": task_id,
                    "base_result_id": existing_task_data.base_result,
                    "manager_name": manager_name,
                }

                failure_parser.handle_completed_output([fail_info])
                current_app.logger.error("update: ERROR\n{}".format(msg))
                task_failures += 1



        current_app.logger.info(
            "QueueManager: Found {} complete tasks ({} successful, {} failed).".format(
                task_totals, task_success, task_failures
            )
        )

        return task_success, task_failures


@main.route("/queue_manager", methods=["GET"])
@check_access
def get_queue_manager():
    """Pulls new tasks from the task queue"""

    body_model, response_model = rest_model("queue_manager", "get")
    body = parse_bodymodel(body_model)

    # Figure out metadata and kwargs
    name = _get_name_from_metadata(body.meta)

    # Grab new tasks and write out
    new_tasks = storage_socket.queue_get_next(
        name, body.meta.programs, body.meta.procedures, limit=body.data.limit, tag=body.meta.tag
    )
    response = response_model(
        **{
            "meta": {
                "n_found": len(new_tasks),
                "success": True,
                "errors": [],
                "error_description": "",
                "missing": [],
            },
            "data": new_tasks,
        }
    )
    # Update manager logs
    storage_socket.manager_update(name, submitted=len(new_tasks), **body.meta.dict())

    return SerializedResponse(response)


@main.route("/queue_manager", methods=["POST"])
@check_access
def post_queue_manager():
    """Posts complete tasks to the task queue"""

    body_model, response_model = rest_model("queue_manager", "post")
    body = parse_bodymodel(body_model)

    success, error = _insert_complete_tasks(storage_socket, body)

    completed = success + error

    response = response_model(
        **{
            "meta": {
                "n_inserted": completed,
                "duplicates": [],
                "validation_errors": [],
                "success": True,
                "errors": [],
                "error_description": "",
            },
            "data": True,
        }
    )

    # Update manager logs
    name = _get_name_from_metadata(body.meta)
    storage_socket.manager_update(name, completed=completed, failures=error)


    return SerializedResponse(response)


@main.route("/queue_manager", methods=["PUT"])
@check_access
def put_queue_manager():
    """
    Various manager manipulation operations
    """

    ret = True

    body_model, response_model = rest_model("queue_manager", "put")
    body = parse_bodymodel(body_model)

    name = _get_name_from_metadata(body.meta)
    op = body.data.operation
    if op == "startup":
        storage_socket.manager_update(
            name, status="ACTIVE", configuration=body.data.configuration, **body.meta.dict(), log=True
        )
        # current_app.logger.info("QueueManager: New active manager {} detected.".format(name))

    elif op == "shutdown":
        nshutdown = storage_socket.queue_reset_status(manager=name, reset_running=True)
        storage_socket.manager_update(
            name, returned=nshutdown, status="INACTIVE", **body.meta.dict(), log=True
        )

        # current_app.logger.info("QueueManager: Shutdown of manager {} detected, recycling {} incomplete tasks.".format(name, nshutdown))

        ret = {"nshutdown": nshutdown}

    elif op == "heartbeat":
        storage_socket.manager_update(name, status="ACTIVE", **body.meta.dict(), log=True)
        # current_app.logger.debug("QueueManager: Heartbeat of manager {} detected.".format(name))

    else:
        msg = "Operation '{}' not understood.".format(op)
        return jsonify(msg=msg), 400

    response = response_model(**{"meta": {}, "data": ret})

    return SerializedResponse(response)


@main.route("/manager", methods=["GET"])
@check_access
def get_manager():
    """Gets manager information from the task queue"""

    body_model, response_model = rest_model("manager", "get")
    body = parse_bodymodel(body_model)

    # current_app.logger.info("GET: ComputeManagerHandler")
    managers = storage_socket.get_managers(**{**body.data.dict(), **body.meta.dict()})

    # remove passwords?
    # TODO: Are passwords stored anywhere else? Other kinds of passwords?
    for m in managers["data"]:
        if "configuration" in m and isinstance(m["configuration"], dict) and "server" in m["configuration"]:
            m["configuration"]["server"].pop("password", None)

    response = response_model(**managers)

    return SerializedResponse(response)


@main.route("/role", methods=["GET"])
@check_access
def get_roles():
    roles = storage_socket.get_roles()
    return jsonify(roles), 200


@main.route("/role/<string:rolename>", methods=["GET"])
@check_access
def get_role(rolename: str):

    success, role = storage_socket.get_role(rolename)
    return jsonify(role), 200


@main.route("/role/<string:rolename>", methods=["POST"])
@check_access
def add_role():
    rolename = request.json["rolename"]
    permissions = request.json["permissions"]

    success, error_message = storage_socket.add_role(rolename, permissions)
    if success:
        return jsonify({"msg": "New role created!"}), 201
    else:
        return jsonify({"msg": error_message}), 400


@main.route("/role", methods=["PUT"])
@check_access
def update_role():
    rolename = request.json["rolename"]
    permissions = request.json["permissions"]

    success = storage_socket.update_role(rolename, permissions)
    if success:
        return jsonify({"msg": "Role was updated!"}), 200
    else:
        return jsonify({"msg": "Failed to update role"}), 400


@main.route("/role", methods=["DELETE"])
@check_access
def delete_role():
    rolename = request.json["rolename"]

    success = storage_socket.delete_role(rolename)
    if success:
        return jsonify({"msg": "Role was deleted!."}), 200
    else:
        return jsonify({"msg": "Filed to delete role!."}), 400