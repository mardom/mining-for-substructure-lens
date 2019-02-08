import sys, os

sys.path.append("../")

import logging
import autograd.numpy as np
import autograd as ag

from units import *
from lensing_sim import LensingSim


class LensingSimulator:
    def __init__(
        self,
        resolution=52,
        coordinate_limit=2.0,
        m_sub_min=1e7 * M_s,
        host_profile="sis",
        host_theta_x=0.01,
        host_theta_y=-0.01,
        host_theta_E=1.0,
        exposure=(1 / 1.8e-19) * erg ** -1 * Centimeter ** 2 * Angstrom * 1000 * Sec,
        A_iso=2e-7 * erg / Centimeter ** 2 / Sec / Angstrom / (radtoasc) ** 2,
        zs=1.0,
        zl=0.1,
        src_profile="sersic",
        src_I_gal=1e-17 * erg / Centimeter ** 2 / Sec / Angstrom,
        src_theta_e_gal=0.5,
        src_n=4,
    ):
        self.resolution = resolution
        self.coordinate_limit = coordinate_limit
        self.m_sub_min = m_sub_min

        # Host galaxy
        self.hst_param_dict = {
            "profile": host_profile,
            "theta_x": host_theta_x,
            "theta_y": host_theta_y,
            "theta_E": host_theta_E,
        }

        # Observational parameters
        self.observation_dict = {
            "nx": resolution,
            "ny": resolution,
            "xlims": coordinate_limit,
            "ylims": coordinate_limit,
            "exposure": exposure,
            "A_iso": A_iso,
        }

        # Global parameters?!
        self.global_dict = {"z_s": zs, "z_l": zl}

        # Source parameters
        self.src_param_dict = {
            "profile": src_profile,
            "I_gal": src_I_gal,
            "theta_e_gal": src_theta_e_gal,
            "n_srsc": src_n,
        }

        # Autograd
        self._d_simulate = ag.grad_and_aux(self._simulate_step)

    def simulate(self, params, params_eval):
        """
        Generates one observed lensed image for given parameters of the subhalo mass distribution
        dn/dm = alpha (m/M_s)^beta with m > m_min and parameters alpha > 0, beta < -1.

        Subhalo coordinates (x,y) are sampled uniformly.
        """

        # Parameters
        alpha = params[0]
        beta = params[1]

        alphas_eval = [alpha] + [param[0] for param in params_eval]
        betas_eval = [beta] + [param[1] for param in params_eval]

        # Joint likelihood
        log_p_xz_eval = [0.0 for _ in alpha_eval]

        # Poisson mean for number of subhalos
        n_sub_mean = -alpha * M_s / (beta + 1) * (self.m_sub_min / M_s) ** (1.0 + beta)

        # Draw number of subhalos
        try:
            n_sub = np.random.poisson(n_sub_mean)
        except ValueError:  # Raised when done in autograd mode for score
            n_sub = np.random.poisson(n_sub_mean._value)

        # Evaluate likelihoods of numbers of subhalos
        for i_eval, (alpha_eval, beta_eval) in enumerate(zip(alphas_eval, betas_eval)):
            n_sub_mean_eval = (
                -alpha_eval
                * M_s
                / (beta_eval + 1)
                * (self.m_sub_min / M_s) ** (1.0 + beta_eval)
            )
            log_p_xz_eval[i_eval] += (
                n_sub * np.log(n_sub_mean_eval) - n_sub_mean_eval
            )  # Can ignore constant term

        # Draw subhalo masses
        u = np.random.uniform(0, 1, size=n_sub)
        m_sub = (self.m_sub_min) * (1 - u) ** (1.0 / (beta + 1.0))

        # Evaluate likelihoods of subhalo masses
        for i_eval, (alpha_eval, beta_eval) in enumerate(zip(alphas_eval, betas_eval)):
            for i_sub in range(n_sub):
                log_p_xz_eval[i_eval] += np.log(-beta_eval - 1.0) + beta_eval * np.log(
                    m_sub[i_sub] / self.m_sub_min
                )

        # Subhalo coordinates
        x_sub = np.random.uniform(
            low=-self.coordinate_limit, high=self.coordinate_limit, size=n_sub
        )
        y_sub = np.random.uniform(
            low=-self.coordinate_limit, high=self.coordinate_limit, size=n_sub
        )

        # Lensing simulation
        lens_list = [self.hst_param_dict]
        for i_sub in range(n_sub):
            sub_param_dict = {
                "profile": "nfw",
                "theta_x": x_sub[i_sub],
                "theta_y": y_sub[i_sub],
                "M200": m_sub[i_sub],
            }
            lens_list.append(sub_param_dict)

        lsi = LensingSim(
            lens_list, [self.src_param_dict], self.global_dict, self.observation_dict
        )
        image_mean = lsi.lensed_image()

        # Observed lensed image
        image = np.random.poisson(image_mean)

        # Returns
        latent_variables = (n_sub, m_sub, x_sub, y_sub, image_mean, image)
        return log_p_xz_eval[0], (image, log_p_xz_eval[1:], latent_variables)

    def rvs(self, alpha, beta, n_images):
        all_images = []

        for i_sim in range(n_images):
            try:
                this_alpha = alpha[i_sim]
                this_beta = beta[i_sim]
            except TypeError:
                this_alpha = alpha
                this_beta = beta
            except IndexError:
                this_alpha = alpha[0]
                this_beta = beta[0]
            params = np.array(this_alpha, this_beta)

            logging.debug(
                "Simulating image %s/%s with alpha = %s, beta = %s",
                i_sim + 1,
                n_images,
                this_alpha,
                this_beta,
            )

            _, (image, _, latents) = self.simulate(params, [])

            n_subhalos = latents[0]
            logging.debug("Image generated with %s subhalos", n_subhalos)

            all_images.append(image)

        return all_images

    def rvs_score_ratio(self, alpha, beta, alpha_ref, beta_ref, n_images):
        all_images = []
        all_t_xz = []
        all_log_r_xz = []

        for i_sim in range(n_images):

            # Prepare parameters
            try:
                this_alpha = alpha[i_sim]
            except TypeError:
                this_alpha = alpha
            except IndexError:
                this_alpha = alpha[0]
            try:
                this_beta = beta[i_sim]
            except TypeError:
                this_beta = beta
            except IndexError:
                this_beta = beta[0]
            try:
                this_alpha_ref = alpha_ref[i_sim]
            except TypeError:
                this_alpha_ref = alpha_ref
            except IndexError:
                this_alpha_ref = alpha_ref[0]
            try:
                this_beta_ref = beta_ref[i_sim]
            except TypeError:
                this_beta_ref = beta_ref
            except IndexError:
                this_beta_ref = beta_ref[0]
            params = np.array(this_alpha, this_beta)
            params_ref = np.array(this_alpha_ref, this_beta_ref)

            logging.debug(
                "Simulating image %s/%s with alpha = %s, beta = %s; also evaluating probability for alpha = %s, beta = %s",
                i_sim + 1,
                n_images,
                this_alpha,
                this_beta,
                this_alpha_ref,
                this_beta_ref,
            )

            t_xz, (image, log_p_xzs, latents) = self.simulate(params, [params_ref])
            log_r_xz = log_p_xzs[0] - log_p_xzs[1]

            n_subhalos = latents[0]
            logging.debug("Image generated with %s subhalos", n_subhalos)

            all_images.append(image)
            all_t_xz.append(t_xz)
            all_log_r_xz.append(log_r_xz)

        return all_images, all_t_xz, all_log_r_xz
