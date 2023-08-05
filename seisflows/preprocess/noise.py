#!/usr/bin/env python3
"""
The SeisFlows Preprocessing module is in charge of interacting with seismic
data (observed and synthetic). It should contain functionality to read and write
seismic data, apply preprocessing such as filtering, quantify misfit,
and write adjoint sources that are expected by the solver.
"""
import os
import numpy as np
from concurrent.futures import ProcessPoolExecutor, wait
from glob import glob
from obspy.geodetics import gps2dist_azimuth

from seisflows import logger
from seisflows.preprocess.default import Default
from seisflows.tools import unix
from seisflows.tools.config import get_task_id


class Noise(Default):
    """
    Noise Preprocess
    ----------------
    Ambient Noise Adjoint Tomography (ANAT) preprocessing functions built ontop
    of the default preprocessing module. Additional functionalities allow for
    rotating and weighting horizontal components (N + E and R + T).

    Parameters
    ----------

    Paths
    -----

    ***
    """
    def __init__(self, **kwargs):
        """
        Preprocessing module parameters

        .. note::
            Paths and parameters listed here are shared with other modules and 
            so are not included in the class docstring.

        :type path_specfem_data: str
        :param path_specfem_data: path to SPECFEM DATA/ directory which must
            contain the CMTSOLUTION, STATIONS and Par_file files used for
            running SPECFEM
        """
        super().__init__(**kwargs)

    def rotate_ne_traces_to_rt(self, source_name, syn_path, data_wildcard, 
                               kernels="RR,TT"):
        """
        Rotates N and E synthetics generated by N and E forward simulations to
        RR and TT component using the (back)azimuth between two stations. Saves
        the resulting waveforms to `solver/{source_name}/traces/syn/*`

        Necessary because the ANAT simulations are performed for N and E forces
        and components, but EGF data is usually given in R and T components to
        isolate Rayleigh and Love waves.

        :type source_name: str
        :param source_name: the name of the source to process
        :type data_wildcard: str
        :param data_wildcard: wildcard string that is used to find waveform
            data. Should match the `solver` module attribute, and have an
            empty string formatter that will be used to specify 'net', 'sta',
            and 'comp'. E.g., '{net}.{sta}.?X{comp}.sem.ascii'
        :type syn_path: str
        :param syn_path: solver-specific synthetic-waveform directory
            which has been wildcarded to allow formatting with a specific force
            to allow access to a given set of synthetics required for rotation.
            Something like "scratch/solver/<source_name>/traces/syn_{}"
            
        :type kernels: str
        :param kernels: comma-separated list of kernels to consider writing
            files for. Saves on file overhead by not writing files that are
            not required. Available are 'TT' and 'RR'. To do both, set as
            'RR,TT' (order insensitive)
        """
        def return_trace_fids(force, component):
            """
            Convenience string return function to get specific synthetic fids
            returns a string that looks something like:
            "scratch/solver/<source_name>/traces/syn_{force}/\
                                                    NN.SSS.?X{component}*
            :type force: str
            :param force: forcesolution direction used to generate synthetics.
                should be lower-case to match workflow.noise_inversion format
            :type component: str
            :param component: synthetic waveform component used for file naming,
                should be upper-case to match SPECFEM requirements
            """
            return sorted(glob(
                os.path.join(syn_path.format(force), 
                             data_wildcard.format(component)))
            )

        # Define the list of file ids and paths required for rotation
        fids_nn = return_trace_fids("n", "N")
        fids_ne = return_trace_fids("n", "E")
        fids_en = return_trace_fids("e", "N")
        fids_ee = return_trace_fids("e", "E")

        assert(len(fids_nn) == len(fids_ne) == len(fids_en) == len(fids_ee)), \
                f"number of synthetic waveforms does not match for all comps"

        # Rotate NE streams to RT in parallel
        with ProcessPoolExecutor(max_workers=unix.nproc()) as executor:
            futures = [
                executor.submit(self._rotate_ne_trace_to_rt, source_name, 
                                f_nn, f_ne, f_en, f_ee, syn_path, kernels)
                for f_nn, f_ne, f_en, f_ee in zip(fids_nn, fids_ne,
                                                  fids_en, fids_ee)
                ]
        # Simply wait until this task is completed because they are file writing
        wait(futures)

    def _rotate_ne_trace_to_rt(self, source_name, f_nn, f_ne, f_en, f_ee,
                               syn_path="./", kernels="RR,TT"):
        """
        Parallellizable function to rotate N and E trace to R and T based on
        a single source station and receiver station pair and their
        respective azimuth values

        .. warning::

            This function makes a lot of assumptions about the directory
            structure and the file naming scheme of synthetics. It is quite
            inflexible and any changes to how SeisFlows treats the solver
            directories or how SPECFEM creates synthetics may break it.

        .. note::

            We are assuming the structure of the filename is something
            like NN.SSS.CCc*, which is standard from SPECFEM

        :type source_name: str
        :param source_name: the name of the source to process
        :type f_nn: str
        :param f_nn: path to the NN synthetic waveform (N force, N component)
        :type f_ne: str
        :param f_ne: path to the NE synthetic waveform (N force, E component)
        :type f_en: str
        :param f_en: path to the EN synthetic waveform (E force, N component)
        :type f_nn: str
        :param f_nn: path to the NN synthetic waveform (N force, N component)
        :type kernels: str
        :param kernels: comma-separated list of kernels to consider writing
            files for. Available are 'TT' and 'RR'. To do both, set as
            'RR,TT' (order insensitive)
        """
        # Define pertinent information about files and output names
        net, sta, cha, *ext = os.path.basename(f_nn).split(".")
        ext = ".".join(ext)  # ['semd', 'ascii'] -> 'semd.ascii.'
        rcv_name = f"{net}_{sta}"

        theta = self.srcrcv_stats.source_name.rcv_name.theta
        theta_p = self.srcrcv_stats.source_name.rcv_name.theta_p

        # Read in the N/E synthetic waveforms that need to be rotated
        # First letter represents the force direction, second is component
        # e.g., ne -> north force recorded on east component
        st_nn = self.read(f_nn, data_format=self.syn_data_format)
        st_ne = self.read(f_ne, data_format=self.syn_data_format)
        st_ee = self.read(f_ee, data_format=self.syn_data_format)
        st_en = self.read(f_en, data_format=self.syn_data_format)

        # We require four waveforms to rotate into the appropriate coord. sys.
        # See Wang et al. (2019) Eqs. 9 and 10 for the rotation matrix def.
        st_tt = st_nn.copy()
        st_rr = st_nn.copy()
        for tr_ee, tr_ne, tr_en, tr_nn, tr_tt, tr_rr in \
                zip(st_ee, st_ne, st_en, st_nn, st_tt, st_rr):
            # TT rotation from Wang et al. (2019) Eq. 9
            tr_tt.data = (+ 1 * np.cos(theta) * np.cos(theta_p) * tr_ee.data
                          - 1 * np.cos(theta) * np.sin(theta_p) * tr_ne.data
                          - 1 * np.sin(theta) * np.cos(theta_p) * tr_en.data
                          + 1 * np.sin(theta) * np.sin(theta_p) * tr_nn.data
                          )
            # RR rotation from Wang et al. (2019) Eq. 10
            tr_rr.data = (+ 1 * np.sin(theta) * np.sin(theta_p) * tr_ee.data
                          - 1 * np.sin(theta) * np.cos(theta_p) * tr_ne.data
                          - 1 * np.cos(theta) * np.sin(theta_p) * tr_en.data
                          + 1 * np.cos(theta) * np.cos(theta_p) * tr_nn.data
                          )

        # !!! Assuming data filename structure here, try make more generic 
        # !!! using parameter `syn_path`
        if "TT" in kernels:
            # scratch/solver/{source_name}/traces/syn/NN.SSS.?XT.sem?*
            fid_t = os.path.join(self.path.solver, source_name, "traces", "syn",
                                 f"{net}.{sta}.{cha[:2]}T.{ext}")
            self.write(st=st_tt, fid=fid_t)
        if "RR" in kernels:
            # scratch/solver/{source_name}/traces/syn/NN.SSS.?XR.sem?*
            fid_r = os.path.join(self.path.solver, source_name, "traces", "syn",
                                 f"{net}.{sta}.{cha[:2]}R.{ext}")
            self.write(st=st_rr, fid=fid_r)

    def rotate_rt_adjsrcs_to_ne(self, source_name, adj_path, choice):
        """
        Rotates N and E synthetics generated by N and E forward simulations to
        RR and TT component using the (back)azimuth between two stations. Saves
        the resulting waveforms to `solver/{source_name}/traces/syn/*`

        Necessary because the ANAT simulations are performed for N and E forces
        and components, but EGF data is usually given in R and T components to
        isolate Rayleigh and Love waves.

        :type source_name: str
        :param source_name: the name of the source to process
        :type data_wildcard: str
        :param data_wildcard: wildcard string that is used to find waveform
            data. Should match the `solver` module attribute, and have an
            empty string formatter that will be used to specify 'net', 'sta',
            and 'comp'. E.g., '{net}.{sta}.?X{comp}.sem.ascii'
        :type adj_path: str
        :param adj_path: solver-specific synthetic-waveform directory
            which has been wildcarded to allow formatting with a specific force
            to allow access to a given set of synthetics required for rotation.
            Something like "scratch/solver/<source_name>/traces/syn_{}"
            
        :type kernels: str
        :param kernels: comma-separated list of kernels to consider writing
            files for. Saves on file overhead by not writing files that are
            not required. Available are 'TT' and 'RR'. To do both, set as
            'RR,TT' (order insensitive)
        """
        assert(choice in ["R", "T"]), f"`choice` must be in 'R', 'T'"

        # Define the list of file ids and paths required for rotation.
        # !!! Hard coding the file naming schema of SPECFEM adjoint sources
        _fid_wc = os.path.join(adj_path, f"*.?X{choice}.adj")
        fids = sorted(glob(_fid_wc))

        # Few checks before providing this to parallel processing
        assert(fids), f"no adjoint sources found for path: {_fid_wc}"
        logger.info(f"rotating {len(fids)} {choice} component adjoint sources")

        # Generate holding directories for rotated adjoint sources which will
        # be queried during each adjoint simulation
        for suffix in [f"_e{choice.lower()}", f"_n{choice.lower()}"]: 
            if not os.path.exists(os.path.abspath(adj_path) + suffix):
                # e.g., path/to/traces/adj_et
                unix.mkdir(os.path.abspath(adj_path) + suffix)  

        # Rotate NE streams to RT in parallel
        with ProcessPoolExecutor(max_workers=unix.nproc()) as executor:
            futures = [
                executor.submit(self._rotate_rt_adjsrc_to_ne,
                                source_name, fid, choice) for fid in fids
                ]
        # Simply wait until this task is completed with file writing
        wait(futures)

    def _rotate_rt_adjsrc_to_ne(self, source_name, fid, choice):
        """
        Parallellizable function to rotate N and E trace to R and T based on
        a single source station and receiver station pair and their
        respective azimuth v# e.g., path/to/traces/adj_etalues

        .. warning::

            This function makes a lot of assumptions about the directory
            structure and the file naming scheme of synthetics. It is quite
            inflexible and any changes to how SeisFlows treats the solver
            directories or how SPECFEM creates synthetics may break it.

        .. note::

            We are assuming the structure of the filename is something
            like NN.SSS.CCc*, which is standard from SPECFEM

        :type source_name: str
        :param source_name: the name of the source to process
        :type fid: str
        :param fid: path to the R or T component adjoint source that will be
            rotated into EE, EN, NE, NN components
        :type choice: str
        :param choice: define the input component, 'R' or 'T' for the incoming
            adjoint source. Also gathered from `fid` but this is used to keep
            things safer and more explicit.
        """
        # Define pertinent information about files and output names
        net, sta, cha, *ext = os.path.basename(fid).split(".")
        ext = ".".join(ext)  # ['semd', 'ascii'] -> 'semd.ascii.'
        assert(cha[-1] == choice), (
                f"Input adjoint source comp '{cha[-1]}' does not match "
                f"choice '{choice}'"
                )
        rcv_name = f"{net}_{sta}"
        theta = self.srcrcv_stats.source_name.rcv_name.theta
        theta_p = self.srcrcv_stats.source_name.rcv_name.theta_p

        # Read in the N/E synthetic waveforms that need to be rotated
        # First letter represents the force direction, second is component
        # e.g., ne -> north force recorded on east component
        st = self.read(fid, data_format=self.syn_data_format)

        # We require four waveforms to rotate into the appropriate coord. sys.
        # See Wang et al. (2019) Eqs. 9 and 10 for the rotation matrix def.
        st_ee = st.copy()
        st_en = st.copy()
        st_ne = st.copy()
        st_nn = st.copy()

        # Assuming that each Stream only has one trace
        for tr_ee, tr_ne, tr_en, tr_nn, tr in \
                zip(st_ee, st_ne, st_en, st_nn, st):
            # TT rotation from Wang et al. (2019) Eq. 16
            if choice == "T":
                tr_ee.data = +1 * np.cos(theta) * np.cos(theta_p) * tr.data
                tr_en.data = -1 * np.cos(theta) * np.sin(theta_p) * tr.data
                tr_ne.data = -1 * np.sin(theta) * np.cos(theta_p) * tr.data
                tr_nn.data = +1 * np.sin(theta) * np.sin(theta_p) * tr.data
            # TT rotation from Wang et al. (2019) Eq. 18
            elif choice == "R":
                tr_ee.data = +1 * np.sin(theta) * np.sin(theta_p) * tr.data
                tr_en.data = +1 * np.sin(theta) * np.cos(theta_p) * tr.data
                tr_ne.data = +1 * np.cos(theta) * np.sin(theta_p) * tr.data
                tr_nn.data = +1 * np.cos(theta) * np.cos(theta_p) * tr.data

        # Lower case for directory naming
        choice = choice.lower()

        # EE and EN are used for the E forcesolution adjoint simulation
        fid_ee = os.path.join(self.path.solver, source_name, "traces", 
                              f"adj_e{choice}", f"{net}.{sta}.{cha[:2]}E.{ext}")

        fid_en = os.path.join(self.path.solver, source_name, "traces", 
                              f"adj_e{choice}", f"{net}.{sta}.{cha[:2]}N.{ext}")

        # NE and NN are used for the N forcesolution adjoint simulation
        fid_ne = os.path.join(self.path.solver, source_name, "traces", 
                              f"adj_n{choice}", f"{net}.{sta}.{cha[:2]}E.{ext}")

        fid_nn = os.path.join(self.path.solver, source_name, "traces", 
                              f"adj_n{choice}", f"{net}.{sta}.{cha[:2]}N.{ext}")

        # Write out all the rotated adjoint sources 
        self.write(st=st_ee, fid=fid_ee)
        self.write(st=st_en, fid=fid_en)
        self.write(st=st_ne, fid=fid_ne)
        self.write(st=st_nn, fid=fid_nn)


