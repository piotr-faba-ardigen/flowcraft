import os
import sys
import json
import jinja2
import logging

from collections import defaultdict
from os.path import dirname, join, abspath, split, splitext, exists, basename


logger = logging.getLogger("main.{}".format(__name__))

try:
    import generator.process as pc
    import generator.market.assembly as assembly
    import generator.market.annotation as annotation
    import generator.market.assembly_processing as ap
    import generator.market.downloads as downloads
    import generator.market.distance_estimation as distest
    import generator.market.mapping as mapping_patlas
    import generator.market.reads_quality_control as readsqc
    import generator.error_handling as eh
    from __init__ import __version__
    from generator import header_skeleton as hs
    from generator import footer_skeleton as fs
    from generator.process_details import colored_print
except ImportError as e:
    import assemblerflow.generator.process as pc
    import assemblerflow.generator.market.assembly as assembly
    import assemblerflow.generator.market.annotation as annotation
    import assemblerflow.generator.market.assembly_processing as ap
    import assemblerflow.generator.market.downloads as downloads
    import assemblerflow.generator.market.distance_estimation as distest
    import assemblerflow.generator.market.mapping as mapping_patlas
    import assemblerflow.generator.market.reads_quality_control as readsqc
    import assemblerflow.generator.process as pc
    import assemblerflow.generator.error_handling as eh
    from assemblerflow import __version__
    from assemblerflow.generator import header_skeleton as hs
    from assemblerflow.generator import footer_skeleton as fs
    from assemblerflow.generator.process_details import colored_print


process_map = {
        "reads_download": downloads.DownloadReads,
        "integrity_coverage": readsqc.IntegrityCoverage,
        "seq_typing": pc.SeqTyping,
        "patho_typing": pc.PathoTyping,
        "check_coverage": readsqc.CheckCoverage,
        "true_coverage": readsqc.TrueCoverage,
        "fastqc": readsqc.FastQC,
        "trimmomatic": readsqc.Trimmomatic,
        "fastqc_trimmomatic": readsqc.FastqcTrimmomatic,
        "skesa": assembly.Skesa,
        "spades": assembly.Spades,
        "process_spades": ap.ProcessSpades,
        "process_skesa": ap.ProcessSkesa,
        "assembly_mapping": ap.AssemblyMapping,
        "pilon": ap.Pilon,
        "mlst": pc.Mlst,
        "abricate": annotation.Abricate,
        "prokka": annotation.Prokka,
        "chewbbaca": pc.Chewbbaca,
        "mash_dist": distest.PatlasMashDist,
        "mash_screen": distest.PatlasMashScreen,
        "mapping_patlas": mapping_patlas.PatlasMapping
}
"""
dict: Maps the process ids to the corresponding template interface class wit
the format::

    {
        "<template_string>": module.TemplateClass
    }
"""


class NextflowGenerator:

    def __init__(self, process_connections, nextflow_file,
                 pipeline_name="assemblerflow", ignore_dependencies=False,
                 auto_dependency=True):

        self.processes = []

        # Create the processes attribute with the first special init process.
        # This process will handle the forks of the raw input channels and
        # secondary inputs
        self.processes = [pc.Init(template="init")]
        """
        list: Stores the process interfaces in the specified order
        """

        self._fork_tree = defaultdict(list)
        """
        dict: A dictionary with the fork tree of the pipeline, which consists
        on the the paths of each lane. For instance, a single fork with two
        sinks is represented as: {1: [2,3]}. Subsequent forks are then added
        sequentially: {1:[2,3], 2:[3,4,5]}. This allows the path upstream
        of a process in a given lane to be traversed until the start of the
        pipeline. 
        """

        self.lanes = 0
        """
        int: Stores the number of lanes in the pipelines
        """

        # Builds the connections in the processes, which parses the
        # process_connections dictionary into the self.processes attribute
        # list.
        self._build_connections(process_connections, ignore_dependencies,
                                auto_dependency)

        self.nf_file = nextflow_file
        """
        str: Path to file where the pipeline will be generated
        """

        self.pipeline_name = pipeline_name
        """
        str: Name of the pipeline, for customization and help purposes.
        """

        self.template = ""
        """
        str: String that will harbour the pipeline code
        """

        self.secondary_channels = {}
        """
        dict: Stores secondary channel links
        """

        self.main_raw_inputs = {}
        """
        list: Stores the main raw inputs from the user parameters into the
        first process(es).
        """

        self.secondary_inputs = {}
        """
        dict: Stores the secondary input channels that may be required by
        some processes. The key is the params variable and the key is the
        channel definition for nextflow::

            {"genomeSize": "IN_genome_size = Channel.value(params.genomeSize)"}

        """

        self.extra_inputs = {}
        """
        """

        self.status_channels = []
        """
        list: Stores the status channels from each process
        """

        self.skip_class = [pc.Compiler]
        """
        list: Stores the Process classes that should be skipped when iterating
        over the :attr:`~NextflowGenerator.processes` list.
        """

        self.resources = ""
        """
        str: Stores the resource directives string for each nextflow process.
        See :func:`NextflowGenerator._get_resources_string`.
        """

        self.containers = ""
        """
        str: Stores the container directives string for each nextflow process.
        See :func:`NextflowGenerator._get_container_string`.
        """

        self.params = ""
        """
        str: Stores the params directives string for the nextflow pipeline.
        See :func:`NextflowGenerator._get_params_string`
        """

        self.user_config = ""
        """
        str: Stores the user configuration file placeholder. This is an
        empty configuration file that is only added the first time to a
        project directory. If the file already exists, it will not overwrite
        it.
        """

    @staticmethod
    def _parse_process_name(name_str):
        """Parses the process string and returns the process name and its
        directives

        Process strings my contain directive information with the following
        syntax::

            proc_name={'directive':'val'}

        This method parses this string and returns the process name as a
        string and the directives information as a dictionary.

        Parameters
        ----------
        name_str : str
            Raw string with process name and, potentially, directive
            information

        Returns
        -------
        str
            Process name
        dict or None
            Process directives
        """

        directives = None

        fields = name_str.split("=")
        process_name = fields[0]

        if len(fields) == 2:
            _directives = fields[1].replace("'", '"')
            try:
                directives = json.loads(_directives)
            except json.decoder.JSONDecodeError:
                raise eh.ProcessError(
                    "Could not parse directives for process '{}'. The raw"
                    " string is: {}\n"
                    "Possible causes include:\n"
                    "\t1. Spaces inside directives\n"
                    "\t2. Missing '=' symbol before directives\n"
                    "\t3. Missing quotes (' or \") around directives\n"
                    "A valid example: process_name={{'cpus':'2'}}".format(
                        process_name, name_str))

        return process_name, directives

    def _build_connections(self, process_list, ignore_dependencies,
                           auto_dependency):
        """Parses the process connections dictionaries into a process list

        This method is called upon instantiation of the NextflowGenerator
        class. Essentially, it sets the main input/output channel names of the
        processes so that they can be linked correctly.

        If a connection between two consecutive process is not possible due
        to a mismatch in the input/output types, it exits with an error.

        Returns
        -------

        """

        logger.debug("=============================")
        logger.debug("Building pipeline connections")
        logger.debug("=============================")

        logger.debug("Processing connections: {}".format(process_list))

        for p, con in enumerate(process_list):

            logger.debug("Processing connection '{}': {}".format(p, con))

            # Get lanes
            in_lane = con["input"]["lane"]
            out_lane = con["output"]["lane"]
            logger.debug("[{}] Input lane: {}".format(p, in_lane))
            logger.debug("[{}] Output lane: {}".format(p, out_lane))

            # Update the total number of lines of the pipeline
            if out_lane > self.lanes:
                self.lanes = out_lane

            # Get process names and directives for the output process
            p_in_name, p_out_name, out_directives = self._get_process_names(
                con, p)

            # Check if process is available or correctly named
            if p_out_name not in process_map:
                logger.error(colored_print(
                    "\nThe process '{}' is not available".format(p_out_name),
                    "red_bold"))
                sys.exit(1)

            # Instance output process
            out_process = process_map[p_out_name](template=p_out_name)

            # Update directives, if provided
            if out_directives:
                out_process.update_attributes(out_directives)

            # Set suffix strings for main input/output channels. Suffixes are
            # based on the lane and the arbitrary and unique process id
            # e.g.: 'process_1_1'
            input_suf = "{}_{}".format(in_lane, p)
            output_suf = "{}_{}".format(out_lane, p)
            logger.debug("[{}] Setting main channels with input suffix '{}'"
                         " and output suffix '{}'".format(
                            p, input_suf, output_suf))
            out_process.set_main_channel_names(input_suf, output_suf, out_lane)

            # Instance input process, if it exists. In case of init, the
            # output process forks from the raw input user data
            if p_in_name != "__init__":
                # Create instance of input process
                in_process = process_map[p_in_name](template=p_in_name)
                # Test if two processes can be connected by input/output types
                logger.debug("[{}] Testing connection between input and "
                             "output processes".format(p))
                self._test_connection(in_process, out_process)
                out_process.parent_lane = in_lane
            else:
                # When the input process is __init__, set the parent_lane
                # to None. This will tell the engine that this process
                # will receive the main input from the raw user input.
                out_process.parent_lane = None

            # If the current connection is a fork, add it to the fork tree
            if in_lane != out_lane:
                logger.debug("[{}] Connection is a fork. Adding lanes to "
                             "fork list".format(p))
                self._fork_tree[in_lane].append(out_lane)
                # Update main output fork of parent process
                try:
                    parent_process = [
                        x for x in self.processes if x.lane == in_lane and
                        x.template == p_in_name
                    ][0]
                    logger.debug(
                        "[{}] Updating main forks of parent fork '{}' with"
                        " '{}'".format(p, parent_process,
                                       out_process.input_channel))
                    parent_process.update_main_forks(out_process.input_channel)
                except IndexError:
                    pass
            else:
                # Get parent process, naive version
                parent_process = self.processes[-1]

                # Check if the last process' lane matches the lane of the
                # current output process. If not, get the last process
                # in the same lane
                if parent_process.lane and parent_process.lane != out_lane:
                    parent_process = [x for x in self.processes[::-1]
                                      if x.lane == out_lane][0]

                if parent_process.output_channel:
                    logger.debug(
                        "[{}] Updating input channel of output process"
                        " with '{}'".format(
                            p, parent_process.output_channel))
                    out_process.input_channel = parent_process.output_channel

            # Check for process dependencies
            if out_process.dependencies and not ignore_dependencies:
                logger.debug("[{}] Dependencies found for process '{}': "
                             "{}".format(p, p_out_name,
                                         out_process.dependencies))
                parent_lanes = self._get_fork_tree(out_lane)
                for dep in out_process.dependencies:
                    if not self._search_tree_backwards(dep, parent_lanes):
                        if auto_dependency:
                            self._add_dependency(
                                out_process, dep, in_lane, out_lane, p)
                        else:
                            logger.error(colored_print(
                                "\nThe following dependency of the process"
                                " '{}' is missing: {}".format(p_out_name, dep),
                                "red_bold"))
                            sys.exit(1)

            self.processes.append(out_process)

        logger.debug("Completed connections: {}".format(self.processes))
        logger.debug("Fork tree: {}".format(self._fork_tree))

    def _get_process_names(self, con, pid):
        """Returns the input/output process names and output process directives

        Parameters
        ----------
        con : dict
            Dictionary with the connection information between two processes.

        Returns
        -------
        input_name : str
            Name of the input process
        output_name : str
            Name of the output process
        output_directives : dict
            Parsed directives from the output process
        """

        try:
            _p_in_name = con["input"]["process"]
            p_in_name, _ = self._parse_process_name(_p_in_name)
            logger.debug("[{}] Input channel: {}".format(pid, p_in_name))
            _p_out_name = con["output"]["process"]
            p_out_name, out_directives = self._parse_process_name(
                _p_out_name)
            logger.debug("[{}] Output channel: {}".format(pid, p_out_name))
        # Exception is triggered when the process name/directives cannot
        # be processes.
        except eh.ProcessError as ex:
            logger.error(colored_print(ex.value, "red_bold"))
            sys.exit(1)

        return p_in_name, p_out_name, out_directives

    def _add_dependency(self, p, template, inlane, outlane, pid):
        """Automatically Adds a dependency of a process.

        This method adds a template to the process list attribute as a
        dependency. It will adapt the input lane, output lane and process
        id of the process that depends on it.

        Parameters
        ----------
        p : Process
            Process class that contains the dependency.
        template : str
            Template name of the dependency.
        inlane : int
            Input lane.
        outlane : int
            Output lane.
        pid : int
            Process ID.
        """

        dependency_proc = process_map[template](template=template)

        if dependency_proc.input_type != p.input_type:
            logger.error("Cannot automatically add dependency with different"
                         " input type. Input type of process '{}' is '{}."
                         " Input type of dependency '{}' is '{}'".format(
                            p.template, p.input_type, template,
                            dependency_proc.input_type))

        input_suf = "{}_{}_dep".format(inlane, pid)
        output_suf = "{}_{}_dep".format(outlane, pid)
        dependency_proc.set_main_channel_names(input_suf, output_suf, outlane)

        # To insert the dependency process before the current process, we'll
        # need to move the input channel name of the later to the former, and
        # set a new connection between the dependency and the process.
        dependency_proc.input_channel = p.input_channel
        p.input_channel = dependency_proc.output_channel

        # If the current process was the first in the pipeline, change the
        # lanes so that the dependency becomes the first process
        if not p.parent_lane:
            p.parent_lane = outlane
            dependency_proc.parent_lane = None
        else:
            dependency_proc.parent_lane = outlane

        self.processes.append(dependency_proc)

    def _search_tree_backwards(self, template, parent_lanes):
        """Searches the process tree backwards in search of a provided process

        The search takes into consideration the provided parent lanes and
        searches only those

        Parameters
        ----------
        template : str
            Name of the process template attribute being searched
        parent_lanes : list
            List of integers with the parent lanes to be searched

        Returns
        -------
        bool
            Returns True when the template is found. Otherwise returns False.
        """

        for p in self.processes[::-1]:

            # Ignore process in different lanes
            if p.lane not in parent_lanes:
                continue

            # template found
            if p.template == template:
                return True

        return False

    @staticmethod
    def _test_connection(parent_process, child_process):
        """Tests if two processes can be connected by input/output type

        Parameters
        ----------
        parent_process : assemblerflow.Process.Process
            Process that will be sending output.
        child_process : assemblerflow.Process.Process
            Process that will receive output.

        """

        # If any of the processes has an ignore type attribute set to True,
        # don't perform the check
        if parent_process.ignore_type or child_process.ignore_type:
            return

        if parent_process.output_type != child_process.input_type:
            logger.error(
                "The output of the '{}' process ({}) cannot link with the "
                "input of the '{}' process ({}). Please check the order of "
                "the processes".format(parent_process.template,
                                       parent_process.output_type,
                                       child_process.template,
                                       child_process.input_type))
            sys.exit(1)

    def _build_header(self):
        """Adds the header template to the master template string
        """

        logger.debug("===============")
        logger.debug("Building header")
        logger.debug("===============")
        self.template += hs.header

    def _build_footer(self):
        """Adds the footer template to the master template string"""

        logger.debug("===============")
        logger.debug("Building header")
        logger.debug("===============")
        self.template += fs.footer

    def _update_raw_input(self, p, sink_channel=None, input_type=None):
        """Given a process, this method updates the
        :attr:`~Process.main_raw_inputs` attribute with the corresponding
        raw input channel of that process. The input channel and input type
        can be overridden if the `input_channel` and `input_type` arguments
        are provided.

        Parameters
        ----------
        p : assemblerflow.Process.Process
            Process instance whose raw input will be modified
        sink_channel: str
            Sets the channel where the raw input will fork into. It overrides
            the process's `input_channel` attribute.
        input_type: str
            Sets the type of the raw input. It overrides the process's
            `input_type` attribute.
        """

        process_input = input_type if input_type else p.input_type
        process_channel = sink_channel if sink_channel else p.input_channel

        logger.debug("[{}] Setting raw input channel "
                     "with input type '{}'".format(p.template, process_input))
        # Get the dictionary with the raw forking information for the
        # provided input
        raw_in = p.get_user_channel(process_channel, process_input)
        logger.debug("[{}] Fetched process raw user: {}".format(p.template,
                                                                raw_in))

        if process_input in self.main_raw_inputs:
            self.main_raw_inputs[process_input]["raw_forks"].append(
                raw_in["input_channel"])
        else:
            self.main_raw_inputs[process_input] = {
                "channel": raw_in["channel"],
                "channel_str": "{}\n{} = {}".format(
                    raw_in["checks"].format(raw_in["params"]),
                    raw_in["channel"],
                    raw_in["channel_str"].format(raw_in["params"])),
                "raw_forks": [raw_in["input_channel"]]
            }
        logger.debug("[{}] Updated main raw inputs: {}".format(
            p.template, self.main_raw_inputs))

    def _update_secondary_inputs(self, p):
        """Given a process, this method updates the
        :attr:`~Process.secondary_inputs` attribute with the corresponding
        secondary inputs of that process.

        Parameters
        ----------
        p : assemblerflow.Process.Process
        """

        logger.debug("[{}] Checking secondary links".format(p.template))
        if p.secondary_inputs:
            logger.debug("[{}] Found secondary input channel(s): "
                         "{}".format(p.template, p.secondary_inputs))
            for ch in p.secondary_inputs:
                if ch["params"] not in self.secondary_inputs:
                    logger.debug("[{}] Added channel: {}".format(
                        p.template, ch["channel"]))
                    self.secondary_inputs[ch["params"]] = ch["channel"]

    def _update_extra_inputs(self, p):
        """Given a process, this method updates the
        :attr:`~Process.extra_inputs` attribute with the corresponding extra
        inputs of that process

        Parameters
        ----------
        p : assemblerflow.Process.Process
        """

        if p.extra_input:
            logger.debug("[{}] Found extra input: {}".format(
                p.template, p.extra_input))

            if p.extra_input == "default":
                # Check if the default type is now present in the main raw
                # inputs. If so, issue an error. The default param can only
                # be used when not present in the main raw inputs
                if p.input_type in self.main_raw_inputs:
                    logger.error(colored_print(
                        "\nThe default input param '{}' of the process '{}'"
                        " is already specified as a main input parameter of"
                        " the pipeline. Please choose a different extra_input"
                        " name.".format(p.input_type, p.template), "red_bold"))
                    sys.exit(1)
                param = p.input_type
            else:
                param = p.extra_input

            dest_channel = "EXTRA_{}_{}".format(p.template, p.pid)

            if param not in self.extra_inputs:
                self.extra_inputs[param] = {
                    "input_type": p.input_type,
                    "channels": [dest_channel]
                }
            else:
                if self.extra_inputs[param]["input_type"] != p.input_type:
                    logger.error(colored_print(
                        "\nThe extra_input parameter '{}' for process"
                        " '{}' was already defined with a different "
                        "input type '{}'. Please choose a different "
                        "extra_input name.".format(
                            p.input_type, p.template,
                            self.extra_inputs[param]["input_type"]),
                        "red_bold"))
                    sys.exit(1)
                self.extra_inputs[param]["channels"].append(dest_channel)

            logger.debug("[{}] Added extra channel '{}' linked to param: '{}' "
                         "".format(p.template, param,
                                   self.extra_inputs[param]))
            p.update_main_input(
                "{}.mix({})".format(p.input_channel, dest_channel)
            )

    def _get_fork_tree(self, lane):
        """

        Parameters
        ----------
        p

        Returns
        -------
        """

        parent_lanes = [lane]

        while True:
            original_lane = lane
            for fork_in, fork_out in self._fork_tree.items():
                if lane in fork_out:
                    lane = fork_in
                    parent_lanes.append(fork_in)
            if lane == original_lane:
                break

        return parent_lanes

    def _set_implicit_link(self, p, link):
        """

        Parameters
        ----------
        p
        link

        Returns
        -------

        """

        output_type = link["link"].lstrip("_")
        parent_forks = self._get_fork_tree(p.lane)
        fork_sink = "{}_{}".format(link["alias"], p.pid)

        for proc in self.processes[::-1]:
            if proc.lane not in parent_forks:
                continue
            if proc.output_type == output_type:
                proc.update_main_forks(fork_sink)
                logger.debug("[{}] Found special implicit link '{}' with "
                             "output type '{}'. Linked '{}' with process "
                             "{}".format(
                                     p.template, link["link"], output_type,
                                     link["alias"], proc))
                return

        self._update_raw_input(p, fork_sink, output_type)

    def _update_secondary_channels(self, p):
        """Given a process, this method updates the
        :attr:`~Process.secondary_channels` attribute with the corresponding
        secondary inputs of that channel.

        The rationale of the secondary channels is the following:

            - Start storing any secondary emitting channels, by checking the
              `link_start` list attribute of each process. If there are
              channel names in the link start, it adds to the secondary
              channels dictionary.
            - Check for secondary receiving channels, by checking the
              `link_end` list attribute. If the link name starts with a
              `__` signature, it will created an implicit link with the last
              process with an output type after the signature. Otherwise,
              it will check is a corresponding link start already exists in
              the at least one process upstream of the pipeline and if so,
              it will update the ``secondary_channels`` attribute with the
              new link.

        Parameters
        ----------
        p : assemblerflow.Process.Process
        """

        # Check if the current process has a start of a secondary
        # side channel
        if p.link_start:
            logger.debug("[{}] Found secondary link start: {}".format(
                p.template, p.link_start))
            for l in p.link_start:
                # If there are multiple link starts in the same lane, the
                # last one is the only one saved.
                if l in self.secondary_channels:
                    self.secondary_channels[l][p.lane] = {"p": p, "end": []}
                else:
                    self.secondary_channels[l] = {p.lane: {"p": p, "end": []}}

        # check if the current process receives a secondary side channel.
        # If so, add to the links list of that side channel
        if p.link_end:
            logger.debug("[{}] Found secondary link end: {}".format(
                p.template, p.link_end))
            for l in p.link_end:

                # Get list of lanes from the parent forks.
                parent_forks = self._get_fork_tree(p.lane)

                # Parse special case where the secondary channel links with
                # the main output of the specified type
                if l["link"].startswith("__"):
                    self._set_implicit_link(p, l)
                    continue

                # Skip if there is no match for the current link in the
                # secondary channels
                if l["link"] not in self.secondary_channels:
                    continue

                for lane in parent_forks:
                    if lane in self.secondary_channels[l["link"]]:
                        self.secondary_channels[
                            l["link"]][lane]["end"].append("{}".format(
                                "{}_{}".format(l["alias"], p.pid)))

        logger.debug("[{}] Secondary links updated: {}".format(
            p.template, self.secondary_channels))

    def _set_channels(self):
        """Sets the main channels for the pipeline

        This method will parse de the :attr:`~Process.processes` attribute
        and perform the following tasks for each process:

            - Sets the input/output channels and main input forks and adds
              them to the process's
              :attr:`assemblerflow.process.Process._context`
              attribute (See
              :func:`~NextflowGenerator.set_channels`).
            - Automatically updates the main input channel of the first
              process of each lane so that they fork from the user provide
              parameters (See
              :func:`~NextflowGenerator._update_raw_input`).
            - Check for the presence of secondary inputs and adds them to the
              :attr:`~NextflowGenerator.secondary_inputs` attribute.
            - Check for the presence of secondary channels and adds them to the
              :attr:`~NextflowGenerator.secondary_channels` attribute.

        Notes
        -----
        **On the secondary channel setup**: With this approach, there can only
        be one secondary link start for each type of secondary link. For
        instance, If there are two processes that start a secondary channel
        for the ``SIDE_max_len`` channel, only the last one will be recorded,
        and all receiving processes will get the channel from the latest
        process. Secondary channels can only link if the source process if
        downstream of the sink process in its "forking" path.
        """

        logger.debug("=====================")
        logger.debug("Setting main channels")
        logger.debug("=====================")

        for i, p in enumerate(self.processes):

            # Set main channels for the process
            logger.debug("[{}] Setting main channels with pid: {}".format(
                p.template, i))
            p.set_channels(pid=i)

            # If there is no parent lane, set the raw input channel from user
            if not p.parent_lane and p.input_type:
                self._update_raw_input(p)

            self._update_secondary_inputs(p)

            self._update_extra_inputs(p)

            self._update_secondary_channels(p)

            logger.info(colored_print(
                "\tChannels set for {} \u2713".format(p.template)))

    def _set_init_process(self):
        """Sets the main raw inputs and secondary inputs on the init process

        This method will fetch the :class:`assemblerflow.process.Init` process
        instance and sets the raw input (
        :func:`assemblerflow.process.Init.set_raw_inputs`) and the secondary
        inputs (:func:`assemblerflow.process.Init.set_secondary_inputs`) for
        that process. This will handle the connection of the user parameters
        with channels that are then consumed in the pipeline.
        """

        logger.debug("========================")
        logger.debug("Setting secondary inputs")
        logger.debug("========================")

        # Get init process
        init_process = self.processes[0]
        logger.debug("Setting main raw inputs: "
                     "{}".format(self.main_raw_inputs))
        init_process.set_raw_inputs(self.main_raw_inputs)
        logger.debug("Setting secondary inputs: "
                     "{}".format(self.secondary_inputs))
        init_process.set_secondary_inputs(self.secondary_inputs)
        logger.debug("Setting extra inputs: {}".format(self.extra_inputs))
        init_process.set_extra_inputs(self.extra_inputs)

    def _set_secondary_channels(self):
        """Sets the secondary channels for the pipeline

        This will iterate over the
        :py:attr:`NextflowGenerator.secondary_channels` dictionary that is
        populated when executing
        :func:`~NextflowGenerator._update_secondary_channels` method.
        """

        logger.debug("==========================")
        logger.debug("Setting secondary channels")
        logger.debug("==========================")

        logger.debug("Setting secondary channels: {}".format(
            self.secondary_channels))

        for source, lanes in self.secondary_channels.items():

            for lane, vals in lanes.items():

                if not vals["end"]:
                    logger.debug("[{}] No secondary links to setup".format(
                        vals["p"].template))
                    continue

                logger.debug("[{}] Setting secondary links for "
                             "source {}: {}".format(vals["p"].template,
                                                    source,
                                                    vals["end"]))

                vals["p"].set_secondary_channel(source, vals["end"])

    def _set_compiler_channels(self):

        self._set_status_channels()

    def _set_status_channels(self):
        """Compiles all status channels for the status compiler process
        """

        status_inst = pc.StatusCompiler(template="status_compiler")
        report_inst = pc.ReportCompiler(template="report_compiler")

        # Compile status channels from pipeline process
        status_channels = []
        for p in [p for p in self.processes]:
            if not any([isinstance(p, x) for x in self.skip_class]):

                status_channels.extend(p.status_strs)

        if not status_channels:
            logger.debug("No status channels found. Skipping status compiler"
                         "process")
            return

        logger.debug("Setting status channels: {}".format(status_channels))

        # Check for duplicate channels. Raise exception if found.
        if len(status_channels) != len(set(status_channels)):
            raise eh.ProcessError(
                "Duplicate status channels detected. Please ensure that "
                "the 'status_channels' attributes of each process are "
                "unique. Here are the status channels:\n\n{}".format(
                    ", ".join(status_channels)
                ))

        status_inst.set_compiler_channels(status_channels)

        report_channels = ["REPORT_{}".format(x.lstrip("STATUS_")) for x in
                           status_channels]

        report_inst.set_compiler_channels(report_channels)

        self.processes.extend([status_inst, report_inst])

    @staticmethod
    def _get_resources_string(res_dict, pid):
        """ Returns the nextflow resources string from a dictionary object

        If the dictionary has at least on of the resource directives, these
        will be compiled for each process in the dictionary and returned
        as a string read for injection in the nextflow config file template.

        This dictionary should be::

            dict = {"processA": {"cpus": 1, "memory": "4GB"},
                    "processB": {"cpus": 2}}

        Parameters
        ----------
        res_dict : dict
            Dictionary with the resources for processes.
        pid : int
            Unique identified of the process

        Returns
        -------
        str
            nextflow config string
        """

        config_str = ""
        ignore_directives = ["container", "version"]

        for p, directives in res_dict.items():

            for d, val in directives.items():

                if d in ignore_directives:
                    continue

                config_str += '\n\t${}_{}.{} = {}'.format(p, pid, d, val)

        return config_str

    @staticmethod
    def _get_container_string(cont_dict, pid):
        """ Returns the nextflow containers string from a dictionary object

        If the dictionary has at least on of the container directives, these
        will be compiled for each process in the dictionary and returned
        as a string read for injection in the nextflow config file template.

        This dictionary should be::

            dict = {"processA": {"container": "asd", "version": "1.0.0"},
                    "processB": {"container": "dsd"}}

        Parameters
        ----------
        cont_dict : dict
            Dictionary with the containers for processes.
        pid : int
            Unique identified of the process

        Returns
        -------
        str
            nextflow config string
        """

        config_str = ""

        for p, directives in cont_dict.items():

            container = ""

            if "container" in directives:
                container += directives["container"]

                if "version" in directives:
                    container += ":{}".format(directives["version"])
                else:
                    container += ":latest"

            config_str += '\n\t${}_{}.container = "{}"'.format(p, pid, container)

        return config_str

    def _get_params_string(self):
        """Returns the nextflow params string from a dictionary object.

        The params dict should be a set of key:value pairs with the
        parameter name, and the default parameter value::

            self.params = {
                "genomeSize": 2.1,
                "minCoverage": 15
            }

        The values are then added to the string as they are. For instance,
        a ``2.1`` float will appear as ``param = 2.1`` and a
        ``"'teste'" string will appear as ``param = 'teste'`` (Note the
        string).

        Returns
        -------
        str
            Nextflow params configuration string
        """

        params_temp = {}

        for p in self.processes:

            logger.debug("[{}] Adding parameters: {}".format(p.template,
                                                             p.params))
            for param, val in p.params.items():

                params_temp[param] = val["default"]

        config_str = "\n\t" + "\n\t".join([
            "{} = {}".format(param, val) for param, val in params_temp.items()
        ])

        return config_str

    def _get_params_help(self):
        """

        Returns
        -------

        """

        help_dict = {}

        for p in self.processes:

            for param, val in p.params.items():

                if param in help_dict:
                    help_dict[param]["process"].append(p.template)
                else:
                    tpl = [p.template] if p.template != "init" else []
                    help_dict[param] = {"process": tpl,
                                        "description": val["description"]}

        # Transform process list into final template string
        for p, val in help_dict.items():
            if not val["process"]:
                val["process"] = ""
            else:
                val["process"] = "({})".format(";".join(val["process"]))

        return help_dict

    @staticmethod
    def _render_config(template, context):

        tpl_dir = join(dirname(abspath(__file__)), "templates")
        tpl_path = join(tpl_dir, template)

        path, filename = split(tpl_path)

        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(path or "./")
        ).get_template(filename).render(context)

    def _set_configurations(self):
        """This method will iterate over all process in the pipeline and
        populate the nextflow configuration files with the directives
        of each process in the pipeline.
        """

        logger.debug("======================")
        logger.debug("Setting configurations")
        logger.debug("======================")

        resources = ""
        containers = ""
        params = ""

        params += self._get_params_string()
        help_dict = self._get_params_help()

        for p in self.processes:

            # Skip processes with the directives attribute populated
            if not p.directives:
                continue

            logger.debug("[{}] Adding directives: {}".format(
                p.template, p.directives))
            resources += self._get_resources_string(p.directives, p.pid)
            containers += self._get_container_string(p.directives, p.pid)

        self.resources = self._render_config("resources.config", {
            "process_info": resources
        })
        self.containers = self._render_config("containers.config", {
            "container_info": containers
        })
        self.params = self._render_config("params.config", {
            "params_info": params
        })
        self.help = self._render_config("Helper.groovy", {
            "nf_file": basename(self.nf_file),
            "help_dict": help_dict,
            "version": __version__,
            "pipeline_name": " ".join([x.upper() for x in self.pipeline_name])
        })
        self.user_config = self._render_config("user.config", {})

    def render_pipeline(self):
        """Write pipeline attributes to json

        This function writes the pipeline and their attributes to a json file,
        that is intended to be read by resources/pipeline_graph.html to render
        a graphical output showing the DAG.

        """

        dict_viz = {
            "name": "root",
            "children": []
        }
        last_of_us = {}

        f_tree = self._fork_tree if self._fork_tree else {1: [1]}

        for x, (k, v) in enumerate(f_tree.items()):
            for p in self.processes[1:]:

                if x == 0 and p.lane not in [k] + v :
                    continue

                if x > 0 and p.lane not in v:
                    continue

                if not p.parent_lane:
                    lst = dict_viz["children"]
                else:
                    lst = last_of_us[p.parent_lane]

                tooltip = {
                    "name": "{}_{}".format(p.template, p.pid),
                    "process": {
                        "pid": p.pid,
                        "input": p.input_type,
                        "output": p.output_type if p.output_type else "None",
                        "lane": p.lane,
                    },
                    "children": []
                }

                dir_var = ""
                for k2, v2 in p.directives.items():
                    dir_var += "<b>&emsp;{}:</b><br>".format(k2)
                    for d in v2:
                        try:
                            dir_var += "&emsp;&emsp;{}: {}</span><br>".\
                                format(d, v2[d])
                        except KeyError:
                            pass

                if dir_var:
                    tooltip["process"]["directives"] = dir_var
                else:
                    tooltip["process"]["directives"] = "N/A"

                lst.append(tooltip)

                last_of_us[p.lane] = lst[-1]["children"]

        # send with jinja to html resource
        return self._render_config("pipeline_graph.html", {"data": dict_viz})

    def write_configs(self, project_root):
        """Wrapper method that writes all configuration files to the pipeline
        directory
        """

        # Write resources config
        with open(join(project_root, "resources.config"), "w") as fh:
            fh.write(self.resources)

        # Write containers config
        with open(join(project_root, "containers.config"), "w") as fh:
            fh.write(self.containers)

        # Write containers config
        with open(join(project_root, "params.config"), "w") as fh:
            fh.write(self.params)

        # Write user config if not present in the project directory
        if not exists(join(project_root, "user.config")):
            with open(join(project_root, "user.config"), "w") as fh:
                fh.write(self.user_config)

        lib_dir = join(project_root, "lib")
        if not exists(lib_dir):
            os.makedirs(lib_dir)
        with open(join(lib_dir, "Helper.groovy"), "w") as fh:
            fh.write(self.help)

        # Generate the pipeline DAG
        pipeline_to_json = self.render_pipeline()
        with open(splitext(self.nf_file)[0] + ".html", "w") as fh:
            fh.write(pipeline_to_json)

    def build(self):
        """Main pipeline builder

        This method is responsible for building the
        :py:attr:`NextflowGenerator.template` attribute that will contain
        the nextflow code of the pipeline.

        First it builds the header, then sets the main channels, the
        secondary inputs, secondary channels and finally the
        status channels. When the pipeline is built, is writes the code
        to a nextflow file.
        """

        logger.info(colored_print(
            "\tSuccessfully connected {} process(es) with {} "
            "fork(s) across {} lane(s) \u2713".format(
                len(self.processes[1:]), len(self._fork_tree), self.lanes)))

        # Generate regular nextflow header that sets up the shebang, imports
        # and all possible initial channels
        self._build_header()

        self._set_channels()

        self._set_init_process()

        logger.info(colored_print(
            "\tSuccessfully set {} secondary input(s) \u2713".format(
                len(self.secondary_inputs))))

        self._set_secondary_channels()

        logger.info(colored_print(
            "\tSuccessfully set {} secondary channel(s) \u2713".format(
                len(self.secondary_channels))))

        self._set_compiler_channels()

        self._set_configurations()

        logger.info(colored_print(
            "\tFinished configurations \u2713"))

        for p in self.processes:
            self.template += p.template_str

        self._build_footer()

        project_root = dirname(self.nf_file)

        # Write configs
        self.write_configs(project_root)

        # Write pipeline file
        with open(self.nf_file, "w") as fh:
            fh.write(self.template)


        logger.info(colored_print(
            "\tPipeline written into {} \u2713".format(self.nf_file)))
